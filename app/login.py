"""OAuth 浏览器登录导入账号。

弹出真实浏览器窗口，用户在页面上自行完成 OAuth 登录
（Google / Apple / 微信 / GitHub / 手机验证码均可），
服务轮询捕获 HttpOnly 的 `token` Cookie，校验后写入账号池。

两种启动模式（可在面板选择）：
  - **纯净模式（推荐）**：不带任何自动化标志启动用户自选浏览器，
    登录全程对 Google 等第三方 IdP 完全正常（不会出现「浏览器不安全」），
    登录完成后通过 CDP 读取 Cookie。
  - **加固模式**：Playwright 驱动，去掉 `--enable-automation` 并注入
    反检测脚本抹掉 `navigator.webdriver` 指纹。

浏览器由用户在面板中选择（自动探测本机已安装的 Chrome / Edge /
Tabbit Browser / Brave / Vivaldi 等）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Optional

from . import browsers as bmod
from .config import ROOT, REGION_PRESETS

logger = logging.getLogger("tabbit2api")

LOGIN_TIMEOUT = 300          # 秒，等待用户完成登录
POLL_INTERVAL = 1.5

LOGIN_DIR = ROOT / "data" / "browser-profiles"


class LoginSession:
    def __init__(self, region: str, base_url: str,
                 browser: Optional[dict] = None, mode: str = "clean"):
        self.id = str(uuid.uuid4())
        self.region = region
        self.base_url = base_url
        self.browser = browser or {}
        self.mode = mode                # clean | stealth
        self.status = "pending"          # pending|waiting|success|failed|cancelled
        self.message = "正在启动浏览器…"
        self.browser_used = ""
        self.started_at = time.time()
        self.finished_at: Optional[float] = None
        self.account_id: Optional[str] = None
        self.display_name = ""
        self.member_level = ""
        self._cancel = asyncio.Event()


class LoginManager:
    """同一时间只允许一个登录会话，避免多个浏览器窗口互相干扰。"""

    def __init__(self, pool):
        self.pool = pool
        self.session: Optional[LoginSession] = None
        self._task: Optional[asyncio.Task] = None

    def status(self) -> dict:
        s = self.session
        if not s:
            return {"active": False}
        return {
            "active": s.status in ("pending", "waiting"),
            "id": s.id, "region": s.region, "base_url": s.base_url,
            "status": s.status, "message": s.message,
            "browser": s.browser_used,
            "started_at": s.started_at,
            "elapsed": round(time.time() - s.started_at, 1),
            "account_id": s.account_id,
            "display_name": s.display_name,
            "member_level": s.member_level,
        }

    def cancel(self) -> bool:
        if self.session and self.session.status in ("pending", "waiting"):
            self.session._cancel.set()
            return True
        return False

    def list_browsers(self) -> list[dict]:
        """供面板展示：本机可用于登录的浏览器。"""
        return bmod.detect_browsers()

    def start(self, region: str, base_url: str | None = None,
              browser_id: str = "", mode: str = "clean") -> dict:
        if self.session and self.session.status in ("pending", "waiting"):
            return {"ok": False, "error": "已有登录会话进行中，请先完成或取消"}
        base_url = (base_url or REGION_PRESETS.get(region, {}).get(
            "base_url", "https://web.tabbit.com")).rstrip("/")
        detected = bmod.detect_browsers()
        chosen = None
        if browser_id:
            chosen = next((b for b in detected
                           if b["id"] == browser_id or b["path"] == browser_id), None)
            if chosen is None:
                return {"ok": False,
                        "error": f"未找到浏览器「{browser_id}」，请刷新浏览器列表"}
        if chosen is None:
            # 默认挑第一个支持 CDP 的（纯净模式需要），否则第一个
            chosen = next((b for b in detected if b.get("chromium")),
                          detected[0] if detected else None)
        if chosen is None:
            return {"ok": False, "error": "本机未检测到可用浏览器"}
        if mode == "clean" and not chosen.get("chromium"):
            return {"ok": False,
                    "error": f"{chosen['label']} 不支持 CDP 读取登录态，"
                             f"请改选 Chrome/Edge/Tabbit 等 Chromium 内核浏览器"}
        s = LoginSession(region, base_url, chosen, mode)
        self.session = s
        self._task = asyncio.create_task(self._run(s))
        return {"ok": True, "session_id": s.id, "base_url": s.base_url,
                "browser": chosen["label"], "mode": mode}

    # ── 主流程 ───────────────────────────────────────────────────
    async def _run(self, s: LoginSession) -> None:
        LOGIN_DIR.mkdir(parents=True, exist_ok=True)
        profile_dir = LOGIN_DIR / f"profile-{s.region}-{int(time.time())}"
        cleanup = {"pw": None, "ctx": None, "proc": None}
        try:
            if s.mode == "clean":
                await self._run_clean(s, profile_dir, cleanup)
            else:
                await self._run_stealth(s, profile_dir, cleanup)
        except Exception as e:
            logger.exception("登录会话异常")
            s.status = "failed"
            s.message = f"登录失败：{e}"
        finally:
            s.finished_at = time.time()
            await _teardown(cleanup, profile_dir)

    # ── 纯净模式：真实浏览器 + CDP 读 Cookie ──────────────────────
    async def _run_clean(self, s: LoginSession, profile_dir: Path,
                         cleanup: dict) -> None:
        login_url = s.base_url + "/login"
        try:
            proc, port = await asyncio.to_thread(
                bmod.launch_clean, s.browser, profile_dir, login_url)
        except Exception as e:
            s.status = "failed"
            s.message = f"启动 {s.browser.get('label','浏览器')} 失败：{e}"
            return
        cleanup["proc"] = proc
        s.browser_used = f"{s.browser['label']}（纯净模式）"
        s.status = "waiting"
        s.message = (f"已用 {s.browser['label']} 打开登录页，"
                     f"请完成登录（此模式不会被 Google 判定为不安全）…")

        token, user_id = await _wait_for_token_cdp(s, port)
        if not token:
            s.status, s.message = (("cancelled", "已取消") if s._cancel.is_set()
                                   else ("failed", "超时未捕获到登录 Cookie"))
            return

        s.message = "已捕获登录态，正在校验…"
        try:
            account = await self._finalize(s, token, user_id)
        except RuntimeError as e:
            s.status, s.message = "failed", str(e)
            return
        s.status = "success"
        s.message = (f"导入成功：{account.display_name or account.name}"
                     f"（{account.member_level or '会员未知'}）")
        s.account_id = account.id
        s.display_name = account.display_name
        s.member_level = account.member_level

    # ── 加固模式：Playwright + 去自动化指纹 ──────────────────────
    async def _run_stealth(self, s: LoginSession, profile_dir: Path,
                           cleanup: dict) -> None:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            s.status = "failed"
            s.message = "未安装 playwright，请先 pip install -r requirements.txt"
            return
        pw = await async_playwright().start()
        cleanup["pw"] = pw
        ctx = None
        last_err: Optional[Exception] = None
        try:
            ctx = await pw.chromium.launch_persistent_context(
                str(profile_dir),
                executable_path=s.browser["path"],
                headless=False,
                args=list(bmod.STEALTH_ARGS),
                ignore_default_args=["--enable-automation",
                                     "--enable-blink-features=IdleDetection"],
                viewport={"width": 1280, "height": 900},
            )
            await ctx.add_init_script(bmod.STEALTH_JS)
        except Exception as e:
            last_err = e
            logger.warning("加固模式启动失败: %s", e)
        if ctx is None:
            s.status = "failed"
            s.message = f"启动 {s.browser.get('label','浏览器')} 失败：{last_err}"
            return
        cleanup["ctx"] = ctx
        s.browser_used = f"{s.browser['label']}（加固模式）"
        s.status = "waiting"
        s.message = f"已用 {s.browser['label']} 打开登录页，请完成登录…"

        page = (ctx.pages or [None])[0] or await ctx.new_page()
        try:
            await page.goto(s.base_url + "/login",
                            wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            s.message = f"打开登录页异常（仍继续等待）: {str(e)[:120]}"

        token, user_id = await _wait_for_token(s, ctx)
        if not token:
            s.status, s.message = (("cancelled", "已取消") if s._cancel.is_set()
                                   else ("failed", "超时未捕获到登录 Cookie"))
            return

        s.message = "已捕获登录态，正在校验…"
        try:
            account = await self._finalize(s, token, user_id)
        except RuntimeError as e:
            s.status, s.message = "failed", str(e)
            return
        s.status = "success"
        s.message = (f"导入成功：{account.display_name or account.name}"
                     f"（{account.member_level or '会员未知'}）")
        s.account_id = account.id
        s.display_name = account.display_name
        s.member_level = account.member_level

    async def _finalize(self, s: LoginSession, token: str, user_id: str):
        from .tabbit_client import TabbitClient, TabbitError

        probe = TabbitClient(base_url=s.base_url, token=token, user_id=user_id)
        # 先验证登录态真实有效，避免把无效凭据写入账号池
        try:
            base_info = await probe.get_base_info()
        except TabbitError as e:
            await probe.aclose()
            raise RuntimeError(
                f"登录态校验失败（HTTP {e.code}）：捕获到的 Cookie 无效或已过期，"
                f"请重新登录") from e
        if not base_info.get("id"):
            await probe.aclose()
            raise RuntimeError("登录态校验失败：未取到用户信息，请重新登录")
        try:
            quota = await probe.get_quota()
        except TabbitError:
            quota = {}
        try:
            models = await probe.get_models()
        except TabbitError:
            models = []
        try:
            await probe.aclose()
        except Exception:
            pass

        display = base_info.get("display_name") or "Tabbit 账号"
        mask = base_info.get("secret_mobile") or base_info.get("secret_email") or ""
        member = quota.get("member_level") or ""
        plans = quota.get("membership_plan_details") or []
        plan = plans[0].get("plan_display_name", "") if plans else ""

        import json as _json
        from .accounts import Account
        from .config import region_for_base_url
        # 以实际登录域名判定归属板块，避免在某个板块里选了别的域名导致错位
        actual_region = region_for_base_url(s.base_url) or s.region
        if actual_region != s.region:
            s.message = (f"登录域名 {s.base_url} 属于"
                         f"{REGION_PRESETS[actual_region]['label']}，已按域名归入该板块")
        blob = _json.dumps({"token": token, "user_id": user_id,
                            "device_id": str(uuid.uuid4()), "next_auth": ""})
        account = Account({
            "name": f"{display}{' · ' + mask if mask else ''}",
            "region": actual_region,
            "base_url": s.base_url,
            "user_id": user_id,
            "device_id": str(uuid.uuid4()),
            "token_cipher": self.pool.cfg.encrypt_token(blob),
            "display_name": display,
            "account_mask": mask,
            "member_level": member,
            "plan_name": plan,
            "usage_percentage": quota.get("usage_percentage", ""),
            "quota": quota,
            "models": models,
            "quota_updated_at": time.time(),
            "status": "active",
        })
        return self.pool.add(account)


# ── 登录态读取与资源清理 ─────────────────────────────────────────
async def _teardown(cleanup: dict, profile_dir: Path) -> None:
    ctx = cleanup.get("ctx")
    if ctx is not None:
        try:
            await ctx.close()
        except Exception:
            pass
    proc = cleanup.get("proc")
    if proc is not None:
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    await asyncio.to_thread(proc.wait, 8)
                except Exception:
                    proc.kill()
        except Exception:
            pass
    pw = cleanup.get("pw")
    if pw is not None:
        try:
            await pw.stop()
        except Exception:
            pass
    shutil.rmtree(profile_dir, ignore_errors=True)


def _cdp_cookies(port: int, base_url: str) -> list[dict]:
    """通过 CDP 读取浏览器 Cookie（纯净模式用，不依赖 Playwright）。"""
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=3) as r:
        targets = json.loads(r.read().decode("utf-8", "ignore"))
    ws_urls = [t.get("webSocketDebuggerUrl") for t in targets
               if t.get("webSocketDebuggerUrl")]
    if not ws_urls:
        return []
    return _cdp_get_cookies(ws_urls[0], base_url)


def _cdp_get_cookies(ws_url: str, base_url: str) -> list[dict]:
    """用原生 WebSocket 发一次 Network.getCookies 请求。"""
    from urllib.parse import urlparse
    import base64
    import socket as _socket
    import ssl
    import struct

    parsed = urlparse(ws_url)
    host, port = parsed.hostname, parsed.port or 80
    path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    sock = _socket.create_connection((host, port), timeout=8)
    try:
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
               f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        sock.sendall(req.encode())
        # 读完握手响应头
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                return []
            buf += chunk
        payload = json.dumps({
            "id": 1, "method": "Network.getCookies",
            "params": {"urls": [base_url]},
        }).encode()
        sock.sendall(_ws_frame(payload))
        # 读一帧响应
        for _ in range(10):
            data = _ws_read(sock)
            if data is None:
                return []
            try:
                msg = json.loads(data)
            except Exception:
                continue
            if msg.get("id") == 1:
                return (msg.get("result") or {}).get("cookies") or []
        return []
    finally:
        try:
            sock.close()
        except Exception:
            pass


def _ws_frame(payload: bytes) -> bytes:
    import struct
    mask = os.urandom(4)
    header = bytearray([0x81])
    n = len(payload)
    if n < 126:
        header.append(0x80 | n)
    elif n < 65536:
        header.append(0x80 | 126)
        header += struct.pack(">H", n)
    else:
        header.append(0x80 | 127)
        header += struct.pack(">Q", n)
    header += mask
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return bytes(header) + masked


def _ws_read(sock) -> bytes | None:
    import struct
    head = sock.recv(2)
    if len(head) < 2:
        return None
    length = head[1] & 0x7F
    if length == 126:
        length = struct.unpack(">H", sock.recv(2))[0]
    elif length == 127:
        length = struct.unpack(">Q", sock.recv(8))[0]
    data = b""
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            return None
        data += chunk
    return data


async def _wait_for_token_cdp(s: LoginSession, port: int) -> tuple[str, str]:
    """纯净模式：轮询 CDP 读 Cookie。"""
    deadline = time.time() + LOGIN_TIMEOUT
    while time.time() < deadline:
        if s._cancel.is_set():
            return "", ""
        try:
            cookies = await asyncio.to_thread(_cdp_cookies, port, s.base_url)
        except Exception:
            cookies = []
        tok = next((c.get("value") for c in cookies
                    if c.get("name") == "token" and c.get("value")), "")
        if tok:
            uid = next((c.get("value") for c in cookies
                        if c.get("name") == "user_id"), "")
            return tok, uid
        s.message = (f"等待登录…（{int(time.time() - s.started_at)}s/"
                     f"{LOGIN_TIMEOUT}s）请在已打开的浏览器里完成 {s.base_url} 的登录")
        await asyncio.sleep(POLL_INTERVAL)
    return "", ""


async def _wait_for_token(s: LoginSession, ctx) -> tuple[str, str]:
    deadline = time.time() + LOGIN_TIMEOUT
    while time.time() < deadline:
        if s._cancel.is_set():
            return "", ""
        try:
            cookies = await ctx.cookies(s.base_url)
        except Exception:
            cookies = []
        tok = next((c["value"] for c in cookies if c.get("name") == "token" and c.get("value")), "")
        if tok:
            uid = next((c["value"] for c in cookies if c.get("name") == "user_id"), "")
            return tok, uid
        s.message = (f"等待登录…（{int(time.time() - s.started_at)}s/"
                     f"{LOGIN_TIMEOUT}s）在浏览器里完成 {s.base_url} 的登录即可")
        await asyncio.sleep(POLL_INTERVAL)
    return "", ""
