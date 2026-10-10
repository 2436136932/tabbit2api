"""Tabbit 上游客户端（协议经实测验证）。

认证：Cookie `token`（HttpOnly JWT）为主，辅以 user_id / managed / SAPISID。
签名：GET /chat/sign-key 取动态 key（各域名独立），对请求体做
     x-nonce = HMAC-SHA256(key, "{ts}.{uuid}.{sha256hex(body)}")，
     x-signature = 随机 uuid，x-timestamp = 毫秒时间戳。
     服务端返回 499 时刷新 key 重试一次。

会话：POST /panel/session 取 chat_session_id；
     POST /api/v1/chat/completion 返回 SSE。免费与付费模型同走 v1，
     无权限模型返回 SSE error code=492。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
import uuid
from typing import Any, AsyncGenerator, Optional

import httpx

from .config import FALLBACK_SIGN_KEY

logger = logging.getLogger("tabbit2api")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)
SEC_CH_UA = '"Tabbit Browser";v="149", "Chromium";v="149", "Not)A;Brand";v="24"'
# 服务端按 `{version}({build})` 解析 x-req-ctx，非此格式会判 493「版本过低」。
# 采用实测通过的值（decodes to "1.6.20(10106020)"）。
DEFAULT_REQ_CTX = "MS42LjIwKDEwMTA2MDIwKQ=="

SIGN_KEY_TTL = 1800  # 秒，过期或 499 时刷新

# 上游 SSE 事件 → 统一事件名
KNOWN_EVENTS = {
    "ready", "title", "message_start", "message_chunk", "message_finish",
    "thinking", "thinking_finished", "switch_model", "usage", "ttft",
    "finish", "close", "error", "cancel", "risky",
}


def md5_empty() -> str:
    return hashlib.md5(b"").hexdigest()


class TabbitError(Exception):
    """上游错误，code 用于区分 401/403/429/492 等。"""

    def __init__(self, message: str, code: int = 500):
        super().__init__(message)
        self.code = code


class TabbitClient:
    """单个账号的上游会话客户端。线程/协程安全级别：单事件循环内复用。"""

    def __init__(self, base_url: str, token: str, user_id: str = "",
                 device_id: str | None = None, client_id: str = "2dd8eb4c1ed9c344d173",
                 req_ctx: str = DEFAULT_REQ_CTX):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.user_id = user_id or self._extract_user_id(token)
        self.device_id = device_id or str(uuid.uuid4())
        self.client_id = client_id
        self.req_ctx = req_ctx
        self._sign_key: str = FALLBACK_SIGN_KEY
        self._sign_key_at: float = 0.0
        self._lock = asyncio.Lock()
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=15, read=300, write=30, pool=30),
            follow_redirects=False,
            verify=False,
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4,
                                keepalive_expiry=60),
        )

    # ── 基础 ─────────────────────────────────────────────────────
    @staticmethod
    def _extract_user_id(token: str) -> str:
        try:
            payload_b64 = token.split(".")[1]
            payload_b64 += "=" * (4 - len(payload_b64) % 4) if len(payload_b64) % 4 else ""
            payload = json.loads(base64.urlsafe_b64decode(payload_b64))
            return str(payload.get("id") or payload.get("sub") or "")
        except Exception:
            return ""

    def _cookies(self) -> dict:
        return {
            "token": self.token,
            "user_id": self.user_id,
            "managed": "tab_browser",
            "NEXT_LOCALE": "zh",
            "SAPISID": self.user_id,
        }

    def _base_headers(self, referer_path: str = "/newtab") -> dict:
        return {
            "User-Agent": UA,
            "sec-ch-ua": SEC_CH_UA,
            "sec-ch-ua-platform": '"Windows"',
            "sec-ch-ua-mobile": "?0",
            "x-chrome-id-consistency-request": (
                f"version=1,client_id={self.client_id},"
                f"device_id={self.device_id},sync_account_id={self.user_id},"
                "signin_mode=all_accounts,signout_mode=show_confirmation"
            ),
            "referer": f"{self.base_url}{referer_path}",
            "Origin": self.base_url,
        }

    def _signature(self, body: str) -> dict:
        ts = str(int(time.time() * 1000))
        nonce_uuid = str(uuid.uuid4())
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        msg = f"{ts}.{nonce_uuid}.{digest}"
        nonce = hmac.new(self._sign_key.encode(), msg.encode(), hashlib.sha256).hexdigest()
        return {"x-timestamp": ts, "x-signature": nonce_uuid, "x-nonce": nonce}

    async def _refresh_sign_key(self) -> None:
        async with self._lock:
            if time.time() - self._sign_key_at < 5:
                return
            try:
                r = await self.client.get(
                    f"{self.base_url}/chat/sign-key",
                    cookies=self._cookies(),
                    headers=self._base_headers("/"),
                )
                if r.status_code == 200 and r.text.strip():
                    self._sign_key = r.text.strip()
                    self._sign_key_at = time.time()
                    logger.debug("sign-key refreshed: %s…", self._sign_key[:8])
            except Exception as e:  # 网络失败保留旧 key
                logger.warning("refresh sign-key failed: %s", e)

    async def ensure_sign_key(self) -> None:
        if time.time() - self._sign_key_at > SIGN_KEY_TTL:
            await self._refresh_sign_key()

    # ── 只读接口 ─────────────────────────────────────────────────
    async def get_models(self) -> list[dict]:
        await self.ensure_sign_key()
        url = f"{self.base_url}/proxy/v1/model_config/models"
        for attempt in range(2):
            headers = {
                **self._base_headers("/newtab"),
                "Accept": "application/json",
                **self._signature(""),
            }
            r = await self.client.get(url, params={"a": "0", "scene": "chat"},
                                      cookies=self._cookies(), headers=headers)
            if r.status_code == 499 and attempt == 0:
                await self._refresh_sign_key()
                continue
            if r.status_code != 200:
                raise TabbitError(f"模型列表失败 {r.status_code}: {r.text[:200]}", r.status_code)
            return r.json().get("models", [])
        raise TabbitError("模型列表失败：签名 key 重试仍失败", 500)

    async def get_base_info(self) -> dict:
        r = await self.client.get(
            f"{self.base_url}/api/v0/user/base-info",
            cookies=self._cookies(),
            headers={**self._base_headers("/"), "Accept": "application/json"},
        )
        if r.status_code != 200:
            raise TabbitError(f"用户信息失败 {r.status_code}", r.status_code)
        return r.json().get("user_info", {})

    async def get_quota(self) -> dict:
        url = f"{self.base_url}/api/commerce/quota/v1/usage"
        r = await self.client.get(url, params={"user_id": self.user_id},
                                  cookies=self._cookies(),
                                  headers={**self._base_headers("/"),
                                           "Accept": "application/json"})
        if r.status_code != 200:
            raise TabbitError(f"额度查询失败 {r.status_code}: {r.text[:200]}", r.status_code)
        return r.json()

    # ── 会话与对话 ───────────────────────────────────────────────
    async def create_session(self) -> str:
        await self.ensure_sign_key()
        headers = {**self._base_headers("/newtab"),
                   "Accept": "application/json",
                   "Pragma": "no-cache",
                   "Cache-Control": "no-cache"}
        r = await self.client.post(f"{self.base_url}/panel/session", content=b"",
                                   headers=headers, cookies=self._cookies())
        if r.status_code != 200:
            raise TabbitError(f"创建会话失败 {r.status_code}: {r.text[:200]}", r.status_code)
        sid = (r.json() or {}).get("chat_session_id", "")
        if not sid:
            raise TabbitError(f"创建会话失败：响应缺少 chat_session_id: {r.text[:200]}", 500)
        return sid

    def _chat_headers(self, session_id: str, body: str) -> dict:
        trace_id = str(uuid.uuid4())
        return {
            **self._base_headers(f"/session/{session_id}"),
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            "Cache-Control": "no-cache",
            "trace-id": trace_id,
            "x-req-ctx": self.req_ctx,
            **self._signature(body),
        }

    def _build_chat_body(self, session_id: str, content: str,
                         model: str) -> str:
        payload = {
            "chat_session_id": session_id,
            "message_id": None,
            "content": content,
            "selected_model": model,
            "parallel_group_id": None,
            "task_name": "chat",
            "agent_mode": False,
            "metadatas": {"html_content": f"<p>{content}</p>"},
            "references": [],
            "entity": {"key": md5_empty(), "extras": {"type": "tab", "url": ""}},
            "client_turn_id": str(uuid.uuid4()),
            "stream_mode": "sse",
            "force_execute": False,
        }
        return json.dumps(payload, ensure_ascii=False)

    async def stream_chat(self, session_id: str, content: str, model: str
                          ) -> AsyncGenerator[dict, None]:
        """发送消息并逐个产出上游 SSE 事件：{"event": name, "data": {...}}。

        自动处理 499（刷新签名 key 重试一次）。
        """
        await self.ensure_sign_key()
        body = self._build_chat_body(session_id, content, model)
        url = f"{self.base_url}/api/v1/chat/completion"

        resp = None
        for attempt in range(2):
            headers = self._chat_headers(session_id, body)
            try:
                req = self.client.build_request(
                    "POST", url, content=body.encode("utf-8"),
                    headers=headers, cookies=self._cookies(),
                )
                resp = await self.client.send(req, stream=True)
            except Exception as e:
                await self.aclose_quiet()
                raise TabbitError(f"上游连接失败: {e}", 502)
            if resp.status_code == 499 and attempt == 0:
                await resp.aclose()
                await self._refresh_sign_key()
                continue
            break

        assert resp is not None
        try:
            if resp.status_code != 200:
                raw = (await resp.aread()).decode("utf-8", "ignore")
                code = resp.status_code
                raise TabbitError(_extract_upstream_error(raw) or f"上游错误 {code}", code)

            current_event: Optional[str] = None
            data_buf: list[str] = []
            async for raw_line in resp.aiter_lines():
                if raw_line.startswith("event:"):
                    current_event = raw_line[len("event:"):].strip()
                elif raw_line.startswith("data:"):
                    data_buf.append(raw_line[len("data:"):].strip())
                elif raw_line.strip() == "":
                    if current_event or data_buf:
                        data_str = "\n".join(data_buf)
                        parsed: Any = None
                        if data_str:
                            try:
                                parsed = json.loads(data_str)
                            except Exception:
                                parsed = {"raw": data_str}
                        yield {"event": current_event or "message_chunk",
                               "data": parsed if isinstance(parsed, dict) else {"raw": parsed}}
                    current_event, data_buf = None, []
        finally:
            await resp.aclose()

    async def aclose_quiet(self) -> None:
        try:
            await self.client.aclose()
        except Exception:
            pass

    async def aclose(self) -> None:
        await self.client.aclose()


def _extract_upstream_error(raw: str) -> str:
    """从非 200 响应体提取人类可读错误信息。"""
    try:
        j = json.loads(raw)
    except Exception:
        return raw[:200] if raw else ""
    for key_path in (("detail", "message"), ("detail",),
                     ("error_message",), ("message",)):
        cur: Any = j
        ok = True
        for k in key_path:
            if isinstance(cur, dict) and k in cur:
                cur = cur[k]
            else:
                ok = False
                break
        if ok and isinstance(cur, str):
            return cur
    return raw[:200]
