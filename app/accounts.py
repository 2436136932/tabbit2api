"""账号存储与多账号池。

- 账号凭据加密落盘（token 为 HttpOnly JWT，属敏感凭据）。
- 池按「区域板块」分组，组内加权轮询 + 冷却/熔断，实现故障切换。
- member_level / quota / models 为从上游拉取的**真实数据**，不做任何伪造。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Optional

from .config import ConfigManager, region_for_base_url
from .tabbit_client import TabbitClient, TabbitError

logger = logging.getLogger("tabbit2api")

TOKEN_TTL_DAYS = 30


class Account:
    def __init__(self, d: dict):
        self.id: str = d.get("id") or str(uuid.uuid4())
        self.name: str = d.get("name") or "账号"
        self.region: str = d.get("region") or "intl"
        self.base_url: str = d.get("base_url")
        self.user_id: str = d.get("user_id") or ""
        self.device_id: str = d.get("device_id") or str(uuid.uuid4())
        self.token_cipher: str = d.get("token_cipher") or ""
        self.enabled: bool = d.get("enabled", True)
        self.status: str = d.get("status", "unknown")
        self.added_at: float = d.get("added_at") or time.time()
        self.last_used_at: Optional[float] = d.get("last_used_at")
        self.last_ok_at: Optional[float] = d.get("last_ok_at")
        self.total_requests: int = d.get("total_requests", 0)
        self.error_count: int = d.get("error_count", 0)
        self.cooldown_until: float = d.get("cooldown_until", 0.0)
        # 真实额度 / 会员信息（来自上游）
        self.display_name: str = d.get("display_name") or ""
        self.account_mask: str = d.get("account_mask") or ""
        self.member_level: str = d.get("member_level") or ""
        self.plan_name: str = d.get("plan_name") or ""
        self.usage_percentage: str = d.get("usage_percentage") or ""
        self.quota: dict = d.get("quota") or {}
        self.models: list[dict] = d.get("models") or []
        self.quota_updated_at: Optional[float] = d.get("quota_updated_at")
        self.last_error: str = d.get("last_error") or ""

    # ── 序列化 ───────────────────────────────────────────────────
    def to_store(self) -> dict:
        return {
            "id": self.id, "name": self.name, "region": self.region,
            "base_url": self.base_url, "user_id": self.user_id,
            "device_id": self.device_id, "token_cipher": self.token_cipher,
            "enabled": self.enabled, "status": self.status,
            "added_at": self.added_at, "last_used_at": self.last_used_at,
            "last_ok_at": self.last_ok_at,
            "total_requests": self.total_requests, "error_count": self.error_count,
            "cooldown_until": self.cooldown_until,
            "display_name": self.display_name, "account_mask": self.account_mask,
            "member_level": self.member_level, "plan_name": self.plan_name,
            "usage_percentage": self.usage_percentage, "quota": self.quota,
            "models": self.models, "quota_updated_at": self.quota_updated_at,
            "last_error": self.last_error,
        }

    def to_public(self) -> dict:
        """对外展示（绝不泄漏 token）。"""
        d = self.to_store()
        d.pop("token_cipher", None)
        d.pop("quota", None)
        # models 是模型元数据（非凭据），面板需要它来渲染模型标签
        d["models"] = self.models or []
        d["models_count"] = len(self.models)
        d["token_set"] = bool(self.token_cipher)
        d["in_cooldown"] = time.time() < self.cooldown_until
        return d

    def available(self) -> bool:
        return self.enabled and bool(self.token_cipher) and time.time() >= self.cooldown_until


class AccountPool:
    def __init__(self, cfg: ConfigManager):
        self.cfg = cfg
        self._accounts: dict[str, Account] = {}
        self._clients: dict[str, TabbitClient] = {}
        self._rr: dict[str, int] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._load()

    # ── 持久化 ───────────────────────────────────────────────────
    def _load(self) -> None:
        self._accounts = {}
        fixed = False
        for d in self.cfg.get("accounts", default=[]):
            a = Account(d)
            # 按登录域名校正板块归属（修复历史版本把 tabbit.com 标成国际版的问题）
            correct = region_for_base_url(a.base_url)
            if correct and correct != a.region:
                logger.info("账号 %s 域名 %s 归属 %s，从 %s 自动校正",
                            a.id[:8], a.base_url, correct, a.region)
                a.region = correct
                fixed = True
            self._accounts[a.id] = a
        if fixed:
            self._persist()

    def _persist(self) -> None:
        self.cfg.set("accounts", [a.to_store() for a in self._accounts.values()])

    # ── CRUD ─────────────────────────────────────────────────────
    def add(self, account: Account) -> Account:
        self._accounts[account.id] = account
        self._persist()
        return account

    def remove(self, account_id: str) -> bool:
        if account_id in self._accounts:
            self._accounts.pop(account_id)
            client = self._clients.pop(account_id, None)
            if client:
                asyncio.create_task(client.aclose())
            self._persist()
            return True
        return False

    def get(self, account_id: str) -> Optional[Account]:
        return self._accounts.get(account_id)

    def list(self, region: str | None = None) -> list[Account]:
        items = [a for a in self._accounts.values()
                 if region is None or a.region == region]
        return sorted(items, key=lambda a: a.added_at)

    def set_enabled(self, account_id: str, enabled: bool) -> bool:
        a = self._accounts.get(account_id)
        if not a:
            return False
        a.enabled = enabled
        if not enabled:
            a.status = "disabled"
        elif a.status == "disabled":
            a.status = "unknown"
        self._persist()
        return True

    def move_region(self, account_id: str, region: str, base_url: str = "") -> bool:
        """把账号移动到另一个板块（可同时切换该板块域名）。"""
        a = self._accounts.get(account_id)
        if not a:
            return False
        a.region = region
        if base_url:
            a.base_url = base_url
        a.status = "unknown"
        a.error_count = 0
        a.cooldown_until = 0.0
        self._clients.pop(account_id, None)   # 域名可能变了，旧客户端作废
        self._persist()
        return True

    def update_token(self, account_id: str, token: str, user_id: str = "",
                     device_id: str = "") -> bool:
        a = self._accounts.get(account_id)
        if not a:
            return False
        blob = json.dumps({"token": token, "user_id": user_id or a.user_id,
                           "device_id": device_id or a.device_id, "next_auth": ""})
        a.token_cipher = self.cfg.encrypt_token(blob)
        a.status = "unknown"
        a.error_count = 0
        a.cooldown_until = 0.0
        self._clients.pop(account_id, None)
        self._persist()
        return True

    def token_of(self, account: Account) -> str:
        blob = self.cfg.decrypt_token(account.token_cipher)
        data = json.loads(blob)
        return data.get("token", "")

    # ── 客户端 ───────────────────────────────────────────────────
    def client_for(self, account: Account) -> TabbitClient:
        c = self._clients.get(account.id)
        if c is None or c.token != self.token_of(account):
            old = self._clients.pop(account.id, None)
            if old:
                asyncio.create_task(old.aclose())
            c = TabbitClient(base_url=account.base_url,
                             token=self.token_of(account),
                             user_id=account.user_id,
                             device_id=account.device_id)
            self._clients[account.id] = c
        return c

    # ── 调度（轮询 + 冷却）───────────────────────────────────────
    def acquire(self, region: str) -> tuple[Optional[Account], Optional[TabbitClient]]:
        candidates = [a for a in self.list(region) if a.available()]
        if not candidates:
            return None, None
        idx = self._rr.get(region, 0) % len(candidates)
        self._rr[region] = (idx + 1) % len(candidates)
        acc = candidates[idx]
        return acc, self.client_for(acc)

    def report_success(self, account_id: str) -> None:
        a = self._accounts.get(account_id)
        if not a:
            return
        a.total_requests += 1
        a.error_count = 0
        a.status = "active"
        a.last_used_at = time.time()
        a.last_ok_at = time.time()
        a.last_error = ""
        self._persist()

    def report_error(self, account_id: str, message: str = "", code: int = 0) -> None:
        a = self._accounts.get(account_id)
        if not a:
            return
        a.total_requests += 1
        a.error_count += 1
        a.last_used_at = time.time()
        a.last_error = message[:300] or f"error {code}"
        max_err = int(self.cfg.get("pool", "max_consecutive_errors", default=3))
        cd = int(self.cfg.get("pool", "cooldown_seconds", default=300))
        if code in (401, 403):
            a.status = "expired"   # 登录态失效，需重新登录
        elif a.error_count >= max_err:
            a.status = "cooldown"
            a.cooldown_until = time.time() + cd
        else:
            a.status = "error"
        self._persist()

    def refresh_from_upstream(self, account_id: str, base_info: dict,
                              quota: dict, models: list[dict]) -> None:
        a = self._accounts.get(account_id)
        if not a:
            return
        a.display_name = (base_info or {}).get("display_name") or a.display_name
        a.account_mask = ((base_info or {}).get("secret_mobile")
                          or (base_info or {}).get("secret_email") or a.account_mask)
        a.member_level = (quota or {}).get("member_level") or a.member_level
        plans = (quota or {}).get("membership_plan_details") or []
        a.plan_name = (plans[0].get("plan_display_name") if plans else "") or a.plan_name
        a.usage_percentage = (quota or {}).get("usage_percentage") or a.usage_percentage
        a.quota = quota or {}
        a.models = models or []
        a.quota_updated_at = time.time()
        self._persist()

    async def close_all(self) -> None:
        for c in list(self._clients.values()):
            try:
                await c.aclose()
            except Exception:
                pass
        self._clients.clear()
