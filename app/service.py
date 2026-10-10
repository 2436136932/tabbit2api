"""会话缓存与对话编排。

同一 (api_key, model, region) 复用上游 chat_session_id，使多轮对话
拥有连续上下文；每会话加锁避免上游 409 冲突。

每次调用都会写入一条用量记录（UsageLog）：模型、账号、token 估算、
倍率折算额度、耗时、TTFT、状态。额度百分比来自上游真实接口，
在调用结束后异步刷新并回填，不阻塞响应。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import AsyncGenerator, Optional

from .accounts import AccountPool, Account
from .tabbit_client import TabbitClient, TabbitError
from .usage_log import UsageLog, estimate_tokens

logger = logging.getLogger("tabbit2api")


class ChatService:
    def __init__(self, pool: AccountPool, cfg, usage: UsageLog | None = None):
        self.pool = pool
        self.cfg = cfg
        self.usage = usage or UsageLog()
        self._sessions: dict[str, dict] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task] = set()
        self._quota_refreshed_at: dict[str, float] = {}

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
    def resolve_model(self, region: str, requested: str
                      ) -> tuple[Optional[str], Optional[dict], str, str]:
        """把请求里的模型名解析为上游 display_name。

        支持 `cn/glm-5.3`、`intl/gpt-6-sol` 前缀写法，也兼容裸名。
        返回 (display_name | None, model_obj | None, region_key, error)。
        """
        requested = (requested or "").strip()
        if not requested:
            requested = self.cfg.get("proxy", "default_model", default="默认") or "默认"
        from .model_catalog import find_model
        m, rk, err = find_model(region, requested)
        if m:
            return m["display_name"], m, (rk or region), ""
        return None, None, (rk or region), err

    def chat(self, bearer: str, model: str, region: str, content: str,
             endpoint: str = "openai", stream: bool = True,
             model_req: str = "") -> AsyncGenerator[dict, None]:
        return self._chat(bearer, model, region, content, endpoint, stream, model_req)

    async def _chat(self, bearer: str, model: str, region: str, content: str,
                    endpoint: str = "openai", stream: bool = True,
                    model_req: str = "") -> AsyncGenerator[dict, None]:
        display, mobj, region, resolve_err = self.resolve_model(region, model)
        if not display:
            # 前缀已指定板块但该板块无账号/无模型 → 报对应板块的账号问题
            if resolve_err:
                raise TabbitError(resolve_err, 400)
            if not self.pool.list(region):
                from .config import REGION_PRESETS
                label = REGION_PRESETS.get(region, {}).get("label", region)
                raise TabbitError(
                    f"「{label}」板块还没有账号，请先到管理面板 "
                    f"http://127.0.0.1:8123/admin 用浏览器登录导入账号", 503)
            raise TabbitError(
                f"未知模型：{model}（可调用 GET /v1/models 查看可用模型，"
                f"支持 cn/、intl/ 前缀）", 400)

        started = time.time()
        first_chunk_at: Optional[float] = None
        out_chars = 0
        acc_used: Optional[Account] = None
        err_text = ""
        status = "ok"

        def _log(acct: Optional[Account]) -> None:
            mult = float((mobj or {}).get("display_multiplier") or 0.0)
            p_tok = estimate_tokens(content)
            c_tok = estimate_tokens("汉" * out_chars) if out_chars else 0
            total = p_tok + c_tok
            self.usage.record(
                endpoint=endpoint, region=region,
                account_id=getattr(acct, "id", "") or "",
                account=getattr(acct, "display_name", "") or "",
                member=getattr(acct, "member_level", "") or "",
                model_req=model_req or model, model=display,
                stream=stream, status=status, error=err_text,
                p_chars=len(content or ""), c_chars=out_chars,
                p_tokens=p_tok, c_tokens=c_tok, total_tokens=total,
                multiplier=mult, credits=round(total * mult, 4),
                ttft_ms=int((first_chunk_at - started) * 1000) if first_chunk_at else 0,
                ms=int((time.time() - started) * 1000),
                api_key=bearer if bearer != "local" else "",
                # 调用时该账号「已看到的」真实额度（零额外请求）；
                # 异步刷新成功后会用最新值回填
                usage_after=getattr(acct, "usage_percentage", "") or "",
            )
            if acct is not None:
                self._spawn_refresh_quota(acct)

        skey = self._key(bearer, display, region)
        async with self._lock_for(skey):
            session_id = self._get_session(skey)
            fresh = session_id is None
            attempts = 0
            got_chunk = False
            try:
                while True:
                    attempts += 1
                    acc, client = self.pool.acquire(region)
                    if acc is None:
                        raise TabbitError(
                            f"区域「{region}」没有可用账号，请到管理面板用浏览器登录导入", 503)
                    acc_used = acc
                    try:
                        if fresh or session_id is None:
                            session_id = await client.create_session()
                            self._set_session(skey, session_id)
                            fresh = False
                        async for ev in client.stream_chat(session_id, content, display):
                            if ev["event"] == "message_chunk":
                                got_chunk = True
                                if first_chunk_at is None:
                                    first_chunk_at = time.time()
                                c = (ev["data"] or {}).get("content")
                                if isinstance(c, str):
                                    out_chars += len(c)
                            elif ev["event"] == "error":
                                status = "upstream_error"
                                d = dict(ev["data"] or {})
                                raw_msg = str(d.get("message") or d)
                                err_text = raw_msg[:300]
                                # 归一化上游误导性文案（见 _explain_error）
                                d["message"] = _explain_error(
                                    raw_msg, d.get("code"), len(content or ""))
                                ev = {"event": "error", "data": d}
                            yield {"account": acc, "event": ev["event"], "data": ev["data"]}
                            if ev["event"] in ("finish", "close", "error", "cancel"):
                                break
                        self.pool.report_success(acc.id)
                        return
                    except TabbitError as e:
                        self.pool.report_error(acc.id, str(e), e.code)
                        err_text = str(e)[:300]
                        if got_chunk or attempts >= len(self.pool.list(region)) or attempts >= 3:
                            status = "error"
                            raise
                        logger.warning("账号 %s 失败(%s)，切换下一个: %s",
                                       acc.id, e.code, e)
                        fresh = True
                        session_id = None
            except Exception:
                if status == "ok":
                    status = "error"
                raise
            finally:
                _log(acc_used)

    # ── 额度刷新（不阻塞响应）─────────────────────────────────────
    QUOTA_REFRESH_MIN_GAP = 60      # 秒，同一账号两次 quota 查询最小间隔

    def _spawn_refresh_quota(self, acc: Account) -> None:
        """调用后异步刷新该账号的真实额度百分比（带节流，避免放大上游请求）。"""
        now = time.time()
        if now - self._quota_refreshed_at.get(acc.id, 0.0) < self.QUOTA_REFRESH_MIN_GAP:
            return
        self._quota_refreshed_at[acc.id] = now
        try:
            task = asyncio.create_task(self._refresh_quota(acc))
        except RuntimeError:
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _refresh_quota(self, acc: Account) -> None:
        try:
            client = self.pool.client_for(acc)
            quota = await client.get_quota()
            acc.member_level = quota.get("member_level") or acc.member_level
            acc.usage_percentage = quota.get("usage_percentage") or acc.usage_percentage
            acc.quota = quota
            self.pool._persist()
            pct = quota.get("usage_percentage") or ""
            if pct:
                # 只更新内存，落盘由定时 flush 负责，避免阻塞事件循环
                self.usage.update_latest(acc.id, usage_after=pct,
                                         persist=False, backfill=5)
        except Exception as e:
            logger.debug("额度刷新失败（忽略）: %s", e)

    async def aclose(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        self.usage.save(force=True)


def _explain_error(raw_msg: str, code, req_chars: int = 0) -> str:
    """把上游误导性错误文案翻译成可操作的真实原因。

    实测：内容超长（≥5 万字符）时上游返回 code=492，文案却是
    「欢迎使用 Tabbit 浏览器…」，与真实原因完全无关。
    """
    msg = (raw_msg or "").strip()
    low = msg.lower()
    # 长度超限：文案是营销语，或请求本身已超阈值
    if "欢迎使用" in msg or "welcome to tabbit" in low:
        if req_chars >= 18000:
            return (f"请求内容过长（约 {req_chars} 字符）被上游拒绝。"
                    f"实测上游单次上限约 2 万字符，请减少上下文或分段发送。"
                    f"（上游原始提示为营销文案，易误导）")
        return ("上游拒绝了本次请求（可能是内容过长或权益校验未通过）。"
                "请减少上下文长度后重试。")
    if "仅高级用户可用" in msg or "升级" in msg:
        return f"{msg}（该模型为 Premium，当前账号等级不足）"
    if "版本过低" in msg:
        return f"{msg}（需更新客户端版本标识）"
    return msg


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
