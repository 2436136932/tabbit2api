"""对外 API Key 鉴权。

管理面板不设密码（打开即用，见 admin_api）；`/v1/*` 的鉴权由面板里
配置的 API Key 控制。

注意：`admin.jwt_secret` 仍然保留 —— 它不再用于面板登录态，而是作为
**账号 token 的加密密钥**（见 config.Crypto），删除会导致已存账号无法解密。
"""
from __future__ import annotations

import hmac

from fastapi import HTTPException


def make_api_key_auth(cfg):
    """对外 API 鉴权：配置了 api_key 则强制 Bearer 匹配，否则放行并返回 'local'。"""
    def auth(authorization: str) -> str:
        required = (cfg.get("proxy", "api_key", default="") or "").strip()
        if not required:
            return "local"
        provided = (authorization or "").strip()
        if provided.lower().startswith("bearer "):
            provided = provided[7:].strip()
        if not provided or not hmac.compare_digest(provided, required):
            raise HTTPException(401, "API Key 无效（Authorization: Bearer <key>）")
        return provided
    return auth
