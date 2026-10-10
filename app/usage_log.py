"""调用记录（用量日志）。

上游 SSE 不返回 usage 事件（实测仅有 ready/message_chunk/thinking/
title/finish/close），因此 token 数为**本地估算**；而额度百分比来自
上游 `/api/commerce/quota/v1/usage`，是**真实值**，会在调用后异步刷新
并作为快照记入本条记录。

记录持久化到 data/usage.json，最多保留 MAX_ENTRIES 条（超出丢弃最旧）。
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Optional

from .config import ROOT

logger = logging.getLogger("tabbit2api")

USAGE_PATH = ROOT / "data" / "usage.json"
MAX_ENTRIES = 2000
SAVE_DEBOUNCE = 2.0     # 秒，合并高频写入


def estimate_tokens(text: str) -> int:
    """粗略估算 token：CJK 字符按 1 个，其余按 4 字符 1 个。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if ord(ch) > 0x2E80)
    return max(1, cjk + (len(text) - cjk) // 4)


class UsageLog:
    def __init__(self, path: Path | None = None):
        self.path = path or USAGE_PATH
        self._items: deque[dict] = deque(maxlen=MAX_ENTRIES)
        self._lock = threading.RLock()      # 可重入，避免持锁时调 save 死锁
        self._dirty = False
        self._last_save = 0.0
        self._load()

    # ── 持久化 ───────────────────────────────────────────────────
    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            for it in data if isinstance(data, list) else []:
                if isinstance(it, dict):
                    self._items.append(it)
            logger.info("已加载 %d 条调用记录", len(self._items))
        except Exception as e:
            logger.warning("调用记录加载失败：%s", e)

    def save(self, force: bool = False) -> None:
        now = time.time()
        with self._lock:
            if not self._dirty and not force:
                return
            if not force and now - self._last_save < SAVE_DEBOUNCE:
                return
            items = list(self._items)
            self._dirty = False
            self._last_save = now
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
        except Exception as e:
            logger.warning("调用记录保存失败：%s", e)

    # ── 写入 ─────────────────────────────────────────────────────
    def record(self, **fields: Any) -> dict:
        item = {
            "id": uuid.uuid4().hex[:12],
            "ts": time.time(),
            "endpoint": fields.get("endpoint", "openai"),
            "region": fields.get("region", ""),
            "account_id": fields.get("account_id", ""),
            "account": fields.get("account", ""),
            "member": fields.get("member", ""),
            "model_req": fields.get("model_req", ""),
            "model": fields.get("model", ""),
            "stream": bool(fields.get("stream", False)),
            "status": fields.get("status", "ok"),
            "error": (fields.get("error") or "")[:300],
            "p_chars": int(fields.get("p_chars", 0)),
            "c_chars": int(fields.get("c_chars", 0)),
            "p_tokens": int(fields.get("p_tokens", 0)),
            "c_tokens": int(fields.get("c_tokens", 0)),
            "total_tokens": int(fields.get("total_tokens", 0)),
            "multiplier": float(fields.get("multiplier") or 0.0),
            "credits": round(float(fields.get("credits") or 0.0), 4),
            "ttft_ms": int(fields.get("ttft_ms") or 0),
            "ms": int(fields.get("ms") or 0),
            "usage_after": fields.get("usage_after", ""),
            "api_key": (fields.get("api_key") or "")[:12],
        }
        with self._lock:
            self._items.append(item)
            self._dirty = True
        # 不在这里同步写盘：避免阻塞事件循环，由 flush 定时落盘
        return item

    # ── 查询 ─────────────────────────────────────────────────────
    def list(self, limit: int = 100, offset: int = 0,
             region: str = "", model: str = "", status: str = "",
             since: float = 0.0) -> dict:
        with self._lock:
            items = list(reversed(self._items))     # 最新在前
        if region:
            items = [i for i in items if i.get("region") == region]
        if model:
            items = [i for i in items
                     if model in (i.get("model") or "")
                     or model in (i.get("model_req") or "")]
        if status:
            items = [i for i in items if i.get("status") == status]
        if since:
            items = [i for i in items if i.get("ts", 0) >= since]
        total = len(items)
        page = items[offset:offset + limit]
        return {"total": total, "items": page, "offset": offset, "limit": limit}

    def stats(self, days: int = 7) -> dict:
        with self._lock:
            items = list(self._items)
        total = len(items)
        ok = sum(1 for i in items if i.get("status") == "ok")
        err = total - ok
        tokens = sum(int(i.get("total_tokens") or 0) for i in items)
        credits = sum(float(i.get("credits") or 0) for i in items)

        by_model: dict[str, dict] = {}
        by_region: dict[str, int] = {}
        by_day: dict[str, dict] = {}
        by_account: dict[str, dict] = {}
        now = time.time()
        cutoff = now - days * 86400

        for i in items:
            m = i.get("model") or i.get("model_req") or "未知"
            b = by_model.setdefault(m, {"model": m, "count": 0, "tokens": 0,
                                        "credits": 0.0, "errors": 0})
            b["count"] += 1
            b["tokens"] += int(i.get("total_tokens") or 0)
            b["credits"] = round(b["credits"] + float(i.get("credits") or 0), 4)
            if i.get("status") != "ok":
                b["errors"] += 1

            r = i.get("region") or "未知"
            by_region[r] = by_region.get(r, 0) + 1

            a = i.get("account") or "未知"
            ab = by_account.setdefault(a, {"account": a, "count": 0, "tokens": 0,
                                           "errors": 0})
            ab["count"] += 1
            ab["tokens"] += int(i.get("total_tokens") or 0)
            if i.get("status") != "ok":
                ab["errors"] += 1

            ts = i.get("ts", 0)
            if ts >= cutoff:
                day = time.strftime("%Y-%m-%d", time.localtime(ts))
                d = by_day.setdefault(day, {"day": day, "count": 0, "tokens": 0,
                                            "errors": 0})
                d["count"] += 1
                d["tokens"] += int(i.get("total_tokens") or 0)
                if i.get("status") != "ok":
                    d["errors"] += 1

        today = time.strftime("%Y-%m-%d", time.localtime(now))
        today_count = by_day.get(today, {}).get("count", 0)
        return {
            "total": total, "ok": ok, "error": err,
            "success_rate": round(ok / total * 100, 1) if total else 0.0,
            "today": today_count,
            "total_tokens": tokens, "total_credits": round(credits, 4),
            "by_model": sorted(by_model.values(), key=lambda x: -x["count"]),
            "by_region": by_region,
            "by_account": sorted(by_account.values(), key=lambda x: -x["count"]),
            "by_day": sorted(by_day.values(), key=lambda x: x["day"])[-days:],
            "note": "token 为本地估算（上游不返回 usage）；额度百分比来自上游真实接口",
        }

    # ── 落盘 ─────────────────────────────────────────────────────
    def flush(self) -> None:
        """供定时任务调用；把待写记录刷到磁盘。"""
        with self._lock:
            dirty = self._dirty
        if dirty:
            self.save(force=True)

    def update_latest(self, account_id: str, persist: bool = True,
                      backfill: int = 1, **fields: Any) -> int:
        """回填该账号最近若干条记录（用于异步刷新的真实额度）。

        默认只回填最新 1 条；backfill>1 时把更早的、额度为空的记录也
        一并填上（并发调用时刷新任务可能晚于响应完成）。

        persist=False 时只更新内存，落盘交给定时 flush（调用方在事件
        循环里，不应同步写文件）。
        """
        filled = 0
        with self._lock:
            for it in reversed(self._items):
                if filled >= backfill:
                    break
                if it.get("account_id") != account_id:
                    continue
                if not fields.get("usage_after") and it.get("usage_after"):
                    continue          # 已有真实值，不覆盖
                it.update(fields)
                filled += 1
            if filled:
                self._dirty = True
        if filled and persist:
            self.save(force=True)
        return filled

    def clear(self) -> int:
        with self._lock:
            n = len(self._items)
            self._items.clear()
            self._dirty = True
        self.save(force=True)
        return n