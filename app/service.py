"""会话缓存与对话编排。

同一 (api_key, model, region) 复用上游 chat_session_id，使多轮对话
拥有连续上下文；每会话加锁避免上游 409 冲突。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import AsyncGenerator, Optional

from .accounts import AccountPool, Account
from .tabbit_client import TabbitClient, TabbitError

logger = logging.getLogger("tabbit2api")


class ChatService:
    def __init__(self, pool: AccountPool, cfg):
        self.pool = pool
        self.cfg = cfg
        self._sessions: dict[str, dict] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    # ── 会话缓存 ─────────────────────────────────────────────────
    def _key(self, bearer: str, model: str, region: str) -> str:
        return f"{bearer}:{model}:{region}"

    def _get_session(self, key: str) -> Optional[str]:
        ttl = int(self.cfg.get("proxy", "session_ttl", default=604800))
        e = self._sessions.get(key)
        if e and time.time() - e["created_at"] < ttl:
            e["last_used"] = time.time()
            e["count"] += 1
            return e["session_id"]
        return None

    def _set_session(self, key: str, session_id: str) -> None:
        ttl = int(self.cfg.get("proxy", "session_ttl", default=0))
        if ttl <= 0:          # 无状态模式：不缓存会话
            return
        self._sessions[key] = {"session_id": session_id, "created_at": time.time(),
                               "last_used": time.time(), "count": 1}

    def drop_session(self, key: str) -> bool:
        return self._sessions.pop(key, None) is not None

    def list_sessions(self) -> list[dict]:
        ttl = int(self.cfg.get("proxy", "session_ttl", default=604800))
        now = time.time()
        out = []
        for k, e in self._sessions.items():
            out.append({"key": k, "session_id": e["session_id"],
                        "created_at": e["created_at"],
                        "last_used": e["last_used"],
                        "count": e["count"],
                        "ttl_remaining": max(0, int(ttl - (now - e["created_at"])))})
        return sorted(out, key=lambda x: x["last_used"], reverse=True)

    def clear_sessions(self) -> int:
        n = len(self._sessions)
        self._sessions.clear()
        return n

    def _lock_for(self, key: str) -> asyncio.Lock:
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    # ── 模型解析 ─────────────────────────────────────────────────
    def resolve_model(self, region: str, requested: str) -> tuple[Optional[str], Optional[dict]]:
        """把请求里的模型名解析为上游 display_name。

        请求可用别名（slug/中文名/display_name），一律映射到上游真实值。
        返回 (display_name | None, model_obj | None)。
        """
        requested = (requested or "").strip()
        if not requested:
            requested = self.cfg.get("proxy", "default_model", default="默认") or "默认"
        from .model_catalog import all_models, find_model
        m = find_model(region, requested)
        if m:
            return m["display_name"], m
        return None, None

    def chat(self, bearer: str, model: str, region: str, content: str
             ) -> AsyncGenerator[dict, None]:
        return self._chat(bearer, model, region, content)

    async def _chat(self, bearer: str, model: str, region: str, content: str
                    ) -> AsyncGenerator[dict, None]:
        display, _mobj = self.resolve_model(region, model)
        if not display:
            if not self.pool.list(region):
                from .config import REGION_PRESETS
                label = REGION_PRESETS.get(region, {}).get("label", region)
                raise TabbitError(
                    f"「{label}」板块还没有账号，请先到管理面板 "
                    f"http://127.0.0.1:8123/admin 用浏览器登录导入账号", 503)
            raise TabbitError(
                f"未知模型：{model}（可调用 GET /v1/models 查看该板块可用模型）", 400)

        skey = self._key(bearer, display, region)
        async with self._lock_for(skey):
            session_id = self._get_session(skey)
            fresh = session_id is None
            attempts = 0
            while True:
                attempts += 1
                acc, client = self.pool.acquire(region)
                if acc is None:
                    raise TabbitError(
                        f"区域「{region}」没有可用账号，请到管理面板用浏览器登录导入", 503)
                try:
                    if fresh or session_id is None:
                        session_id = await client.create_session()
                        self._set_session(skey, session_id)
                        fresh = False
                    got_chunk = False
                    async for ev in client.stream_chat(session_id, content, display):
                        if ev["event"] == "message_chunk":
                            got_chunk = True
                        yield {"account": acc, "event": ev["event"], "data": ev["data"]}
                        if ev["event"] in ("finish", "close", "error", "cancel"):
                            break
                    self.pool.report_success(acc.id)
                    return
                except TabbitError as e:
                    self.pool.report_error(acc.id, str(e), e.code)
                    if got_chunk or attempts >= len(self.pool.list(region)) or attempts >= 3:
                        raise
                    logger.warning("账号 %s 失败(%s)，切换下一个: %s",
                                   acc.id, e.code, e)
                    fresh = True
                    session_id = None


def collect_text(events: list[dict]) -> tuple[str, str]:
    """把已收集事件拼成 (content, reasoning)。"""
    content, reasoning = [], []
    for ev in events:
        d = ev.get("data") or {}
        if ev["event"] == "message_chunk":
            c = d.get("content")
            if isinstance(c, str):
                content.append(c)
        elif ev["event"] == "thinking":
            r = d.get("reasoning_content")
            if isinstance(r, str):
                reasoning.append(r)
    return "".join(content), "".join(reasoning)
