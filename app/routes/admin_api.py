"""管理面板后台 API：账号、登录会话、额度刷新、设置。

注意：本文件不使用 `from __future__ import annotations`，
FastAPI 对路由/依赖参数需要可直接求值的注解。
"""
import time
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..config import REGION_PRESETS
from ..model_catalog import get_catalog, set_models
from ..tabbit_client import TabbitClient, TabbitError

router = APIRouter(prefix="/admin/api")


class RegionReq(BaseModel):
    region: str
    base_url: Optional[str] = None
    browser_id: Optional[str] = None
    mode: Optional[str] = "clean"


class SettingReq(BaseModel):
    api_key: Optional[str] = None
    default_model: Optional[str] = None
    session_ttl: Optional[int] = None
    cooldown_seconds: Optional[int] = None
    max_consecutive_errors: Optional[int] = None


class AccountReq(BaseModel):
    name: Optional[str] = None
    enabled: Optional[bool] = None
    region: Optional[str] = None
    base_url: Optional[str] = None


def setup(state) -> APIRouter:
    cfg = state["config"]
    pool = state["pool"]
    service = state["service"]
    logins = state["logins"]

    # ── 区域板块 ─────────────────────────────────────────────────
    @router.get("/regions")
    async def regions():
        out = []
        for key, preset in REGION_PRESETS.items():
            cur = cfg.get("regions", key, default={}) or {}
            out.append({"key": key,
                        "label": cur.get("label") or preset["label"],
                        "base_url": cur.get("base_url") or preset["base_url"],
                        "alt_urls": preset.get("alt_urls", []),
                        "accounts": len(pool.list(key)),
                        "models_updated_at": get_catalog(key).get("updated_at")})
        return {"regions": out}

    @router.post("/regions/{key}/base-url")
    async def set_region_url(key: str, req: dict):
        if key not in REGION_PRESETS:
            raise HTTPException(404, "未知区域")
        url = (req.get("base_url") or "").strip().rstrip("/")
        if url and not url.startswith(("http://", "https://")):
            raise HTTPException(400, "URL 需以 http(s):// 开头")
        cfg.set("regions", key, "base_url", url or REGION_PRESETS[key]["base_url"])
        return {"ok": True, "base_url": cfg.get("regions", key, "base_url")}

    # ── 浏览器 OAuth 登录 ────────────────────────────────────────
    @router.get("/browsers")
    async def list_browsers():
        """本机可用于登录的浏览器（供面板下拉选择）。"""
        return {"browsers": logins.list_browsers()}

    @router.post("/login/browser")
    async def start_browser_login(req: RegionReq):
        if req.region not in REGION_PRESETS:
            raise HTTPException(400, "region 需为 cn 或 intl")
        base = req.base_url or cfg.get("regions", req.region, "base_url")
        mode = (req.mode or "clean").lower()
        if mode not in ("clean", "stealth"):
            raise HTTPException(400, "mode 需为 clean 或 stealth")
        return logins.start(req.region, base, req.browser_id or "", mode)

    @router.get("/login/status")
    async def login_status():
        return logins.status()

    @router.post("/login/cancel")
    async def login_cancel():
        return {"ok": logins.cancel()}

    # ── 账号 ─────────────────────────────────────────────────────
    @router.get("/accounts")
    async def list_accounts():
        out = []
        for region in REGION_PRESETS:
            for a in pool.list(region):
                d = a.to_public()
                d["region_label"] = REGION_PRESETS[region]["label"]
                out.append(d)
        return {"accounts": out}

    @router.patch("/accounts/{account_id}")
    async def patch_account(account_id: str, req: AccountReq):
        acc = pool.get(account_id)
        if not acc:
            raise HTTPException(404, "账号不存在")
        if req.enabled is not None:
            pool.set_enabled(account_id, req.enabled)
        if req.region is not None:
            if req.region not in REGION_PRESETS:
                raise HTTPException(400, "region 需为 cn 或 intl")
            base = req.base_url or cfg.get("regions", req.region, "base_url")
            pool.move_region(account_id, req.region, base)
            if acc.models:                     # 迁移后并入新板块目录
                set_models(req.region, acc.models)
        return {"ok": True, "account": acc.to_public()}

    @router.get("/regions/{key}/models")
    async def region_models(key: str):
        """面板专用模型列表：走服务内部目录，不需要代理 API Key。"""
        if key not in REGION_PRESETS:
            raise HTTPException(404, "未知区域")
        from ..model_catalog import all_models
        models = all_models(key)
        return {"region": key, "count": len(models),
                "updated_at": get_catalog(key).get("updated_at"),
                "models": models}

    @router.delete("/accounts/{account_id}")
    async def del_account(account_id: str):
        if not pool.remove(account_id):
            raise HTTPException(404, "账号不存在")
        return {"ok": True}

    @router.post("/accounts/{account_id}/refresh")
    async def refresh_account(account_id: str):
        acc = pool.get(account_id)
        if not acc:
            raise HTTPException(404, "账号不存在")
        if not acc.token_cipher:
            raise HTTPException(400, "账号缺少令牌")
        client = pool.client_for(acc)
        try:
            base_info = await client.get_base_info()
        except TabbitError as e:
            pool.report_error(account_id, str(e), e.code)
            raise HTTPException(e.code or 502, f"用户信息失败：{e}")
        try:
            quota = await client.get_quota()
        except TabbitError as e:
            pool.report_error(account_id, str(e), e.code)
            raise HTTPException(e.code or 502, f"额度查询失败：{e}")
        try:
            models = await client.get_models()
        except TabbitError:
            models = []
        pool.refresh_from_upstream(account_id, base_info, quota, models)
        set_models(acc.region, models)
        return {"ok": True, "account": acc.to_public()}

    @router.post("/accounts/{account_id}/test")
    async def test_account(account_id: str):
        acc = pool.get(account_id)
        if not acc:
            raise HTTPException(404, "账号不存在")
        client = pool.client_for(acc)
        try:
            sid = await client.create_session()
            pool.report_success(account_id)
            return {"ok": True, "session_id": sid}
        except TabbitError as e:
            pool.report_error(account_id, str(e), e.code)
            return {"ok": False, "error": str(e), "code": e.code}

    @router.get("/accounts/{account_id}/models")
    async def account_models(account_id: str):
        acc = pool.get(account_id)
        if not acc:
            raise HTTPException(404, "账号不存在")
        return {"models": acc.models, "member_level": acc.member_level,
                "plan_name": acc.plan_name}

    @router.post("/regions/{key}/refresh-models")
    async def refresh_region_models(key: str):
        if key not in REGION_PRESETS:
            raise HTTPException(404, "未知区域")
        accounts = pool.list(key)
        if not accounts:
            raise HTTPException(400, f"「{REGION_PRESETS[key]['label']}」还没有可用账号")
        last_err = None
        for acc in accounts:
            if not acc.token_cipher:
                continue
            try:
                models = await pool.client_for(acc).get_models()
                set_models(key, models)
                pool.refresh_from_upstream(acc.id, {}, {}, models)
                return {"ok": True, "count": len(models)}
            except TabbitError as e:
                last_err = e
        raise HTTPException(502, f"刷新模型失败：{last_err}")

    # ── 会话 ─────────────────────────────────────────────────────
    @router.get("/sessions")
    async def sessions():
        return {"sessions": service.list_sessions()}

    @router.delete("/sessions")
    async def clear_sessions():
        return {"cleared": service.clear_sessions()}

    # ── 设置 ─────────────────────────────────────────────────────
    @router.get("/settings")
    async def get_settings():
        return {
            "api_key": cfg.get("proxy", "api_key", default=""),
            "default_model": cfg.get("proxy", "default_model", default="默认"),
            "session_ttl": cfg.get("proxy", "session_ttl", default=0),
            "cooldown_seconds": cfg.get("pool", "cooldown_seconds", default=300),
            "max_consecutive_errors": cfg.get("pool", "max_consecutive_errors", default=3),
            "port": cfg.get("server", "port", default=8123),
        }

    @router.put("/settings")
    async def put_settings(req: SettingReq):
        if req.api_key is not None:
            cfg.set("proxy", "api_key", req.api_key.strip())
        if req.default_model is not None:
            cfg.set("proxy", "default_model", req.default_model.strip())
        if req.session_ttl is not None:
            cfg.set("proxy", "session_ttl", max(0, int(req.session_ttl)))
        if req.cooldown_seconds is not None:
            cfg.set("pool", "cooldown_seconds", max(10, int(req.cooldown_seconds)))
        if req.max_consecutive_errors is not None:
            cfg.set("pool", "max_consecutive_errors", max(1, int(req.max_consecutive_errors)))
        return {"ok": True}

    @router.get("/overview")
    async def overview():
        accs = pool.list()
        return {
            "accounts_total": len(accs),
            "accounts_active": sum(1 for a in accs if a.available()),
            "sessions": len(service.list_sessions()),
            "port": cfg.get("server", "port", default=8123),
            "has_api_key": bool(cfg.get("proxy", "api_key", default="")),
        }

    return router
