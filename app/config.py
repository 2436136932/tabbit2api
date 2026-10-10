"""配置管理。

config.json 保存账号、API Key、区域板块等持久化状态。
令牌属于敏感凭据，落盘前用 Fernet 风格对称密钥加密（缺省用 cryptography，
未安装时退化为 base64 混淆并给出告警）。
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import secrets
import sys
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"

# ── 区域板块默认域名 ────────────────────────────────────────────
# 依据前端 chunk 的权威映射（20251-*.js / page-*.js）：
#   tabbitCn: "tabbit.com"           → 国内版
#   tabbitCnLegacy: "tabbit-ai.com"  → 国内版（遗留域名）
#   "domestic" → https://www.tabbit.com ; "overseas" → https://www.tabbit.ai
# 实测 web.tabbit.ai 在国内网络返回 403 "Service Unavailable in Your Region"。
REGION_PRESETS = {
    "cn": {
        "label": "国内版",
        "base_url": "https://web.tabbit.com",
        "alt_urls": ["https://web.tabbit-ai.com"],
    },
    "intl": {
        "label": "国际版",
        "base_url": "https://web.tabbit.ai",
        "alt_urls": ["https://www.tabbit.ai"],
    },
}

# 域名 → 板块，用于把已有账号按登录域名自动归位
_DOMESTIC_HOSTS = {"web.tabbit.com", "www.tabbit.com", "tabbit.com",
                   "web.tabbit-ai.com", "www.tabbit-ai.com", "tabbit-ai.com"}
_OVERSEAS_HOSTS = {"web.tabbit.ai", "www.tabbit.ai", "tabbit.ai",
                   "web.tabbitbrowser.com", "tabbitbrowser.com"}


def region_for_base_url(url: str) -> str | None:
    """由登录域名推断所属板块（国内版 cn / 国际版 intl）。"""
    if not url:
        return None
    try:
        from urllib.parse import urlparse
        host = (urlparse(url).netloc or "").lower().split(":")[0]
    except Exception:
        return None
    if host in _DOMESTIC_HOSTS:
        return "cn"
    if host in _OVERSEAS_HOSTS:
        return "intl"
    return None

FALLBACK_SIGN_KEY = "f8d0e6a73f8d4b1a9c3d2e1f9a4b7c6d"

DEFAULT_CONFIG: dict[str, Any] = {
    "server": {"port": 8123, "host": "0.0.0.0"},
    "proxy": {"api_key": "", "default_model": "默认", "session_ttl": 0},
    # 面板无密码登录；jwt_secret 仅用作账号 token 的加密密钥（勿删，否则已存账号无法解密）
    "admin": {"jwt_secret": ""},
    "regions": {
        "cn": {"label": "国内版", "base_url": REGION_PRESETS["cn"]["base_url"]},
        "intl": {"label": "国际版", "base_url": REGION_PRESETS["intl"]["base_url"]},
    },
    "accounts": [],
    "pool": {
        "cooldown_seconds": 300,
        "max_consecutive_errors": 3,
        "retry_429_seconds": 15,
    },
}

class Crypto:
    """令牌加密。优先 cryptography.Fernet，否则退化为 base64+XOR。"""

    def __init__(self, secret: str):
        self.secret = secret.encode() if secret else b"tabbit2api-default-secret"

    def _fernet(self):
        try:
            from cryptography.fernet import Fernet

            digest = hashlib.sha256(self.secret).digest()
            return Fernet(base64.urlsafe_b64encode(digest))
        except Exception:
            return None

    def encrypt(self, plain: str) -> str:
        f = self._fernet()
        if f:
            return "enc1:" + f.encrypt(plain.encode()).decode()
        raw = plain.encode()
        keystream = (self.secret * ((len(raw) // len(self.secret)) + 1))[: len(raw)]
        xored = bytes(a ^ b for a, b in zip(raw, keystream))
        return "b64:" + base64.urlsafe_b64encode(xored).decode()

    def decrypt(self, cipher: str) -> str:
        if cipher.startswith("enc1:"):
            f = self._fernet()
            if f:
                return f.decrypt(cipher[5:].encode()).decode()
            raise ValueError("cryptography 未安装，无法解密 enc1 令牌")
        if cipher.startswith("b64:"):
            xored = base64.urlsafe_b64decode(cipher[4:])
            keystream = (self.secret * ((len(xored) // len(self.secret)) + 1))[: len(xored)]
            return bytes(a ^ b for a, b in zip(xored, keystream)).decode()
        return cipher  # 明文（历史遗留）


class ConfigManager:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else CONFIG_PATH
        self.config = self._load()
        self.crypto = Crypto(self.get("admin", "jwt_secret", default="") or "tabbit2api")

    # ── 加载 / 保存 ──────────────────────────────────────────────
    def _load(self) -> dict:
        if self.path.exists():
            try:
                saved = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                saved = {}
            config = _deep_merge(copy.deepcopy(DEFAULT_CONFIG), saved)
            # 旧配置可能残留面板密码字段，清理掉（面板已无密码登录）
            for k in ("username", "password_hash", "salt"):
                config.get("admin", {}).pop(k, None)
            if not config.get("admin", {}).get("jwt_secret"):
                config["admin"]["jwt_secret"] = secrets.token_hex(32)
        else:
            config = copy.deepcopy(DEFAULT_CONFIG)
            config["admin"]["jwt_secret"] = secrets.token_hex(32)
        self._save(config)
        return config

    def _save(self, config: dict | None = None) -> None:
        config = config or self.config
        self.path.write_text(
            json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def save(self) -> None:
        self._save()

    # ── 读写 ─────────────────────────────────────────────────────
    def get(self, *keys: str, default: Any = None) -> Any:
        val: Any = self.config
        for k in keys:
            if not isinstance(val, dict):
                return default
            val = val.get(k)
            if val is None:
                return default
        return val

    def set(self, *keys_and_value: Any) -> None:
        keys, value = keys_and_value[:-1], keys_and_value[-1]
        d = self.config
        for k in keys[:-1]:
            d = d.setdefault(k, {})
        d[keys[-1]] = value
        self.save()

    # ── 便捷 ─────────────────────────────────────────────────────
    @property
    def accounts(self) -> list[dict]:
        return self.config.setdefault("accounts", [])

    def encrypt_token(self, token: str) -> str:
        return self.crypto.encrypt(token)

    def decrypt_token(self, cipher: str) -> str:
        return self.crypto.decrypt(cipher)

    def next_free_port(self, start: int, host: str = "127.0.0.1") -> int:
        """从 start 起探测空闲端口（冲突则 +1）。

        注意：探测 socket 不能设 SO_REUSEADDR —— Windows 上它会允许
        重复绑定别的进程已占用的端口，导致探测失真。
        """
        import socket

        port = start
        for _ in range(50):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                try:
                    s.bind((host, port))
                    return port
                except OSError:
                    port += 1
        return start


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


# 早期版本写反的板块默认域名 → 修正为正确值
_WRONG_DEFAULTS = {
    ("cn", "https://web.tabbit-ai.com"): "https://web.tabbit.com",
    ("intl", "https://web.tabbit.com"): "https://web.tabbit.ai",
}


def migrate_config(cfg: "ConfigManager") -> list[str]:
    """修正历史版本写错的板块域名（仅当值恰好等于旧默认值时才改）。"""
    changes = []
    for (key, wrong), right in _WRONG_DEFAULTS.items():
        cur = (cfg.get("regions", key, "base_url", default="") or "").rstrip("/")
        if cur == wrong:
            cfg.set("regions", key, "base_url", right)
            changes.append(f"regions.{key}.base_url: {wrong} -> {right}")
    return changes


def warn(msg: str) -> None:
    print(f"[tabbit2api] {msg}", file=sys.stderr)
