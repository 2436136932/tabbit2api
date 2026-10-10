"""应用装配与启动。

端口从 8123 起自动探测空闲端口（冲突则 +1），不使用 Docker。
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse

from .accounts import AccountPool
from .auth import make_api_key_auth
from .config import ConfigManager, ROOT, warn
from .login import LoginManager
from .service import ChatService
from .usage_log import UsageLog

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("tabbit2api")

STATIC = Path(__file__).resolve().parent / "static"


def create_app(cfg: ConfigManager | None = None) -> FastAPI:
    cfg = cfg or ConfigManager()
    # 修正历史版本写反的板块域名（国内版=web.tabbit.com / 国际版=web.tabbit.ai）
    from .config import migrate_config
    for line in migrate_config(cfg):
        logger.info("配置迁移 %s", line)
    pool = AccountPool(cfg)
    usage = UsageLog()
    service = ChatService(pool, cfg, usage)
    logins = LoginManager(pool)

    app = FastAPI(title="Tabbit2API", docs_url=None, redoc_url=None)
    state = {"config": cfg, "pool": pool, "service": service,
             "logins": logins, "auth": make_api_key_auth(cfg), "usage": usage}

    from .routes import admin_api, claude_api, openai_api
    app.include_router(openai_api.setup(state))
    app.include_router(claude_api.setup(state))
    app.include_router(admin_api.setup(state))

    @app.get("/", include_in_schema=False)
    async def index():
        return RedirectResponse("/admin")

    @app.get("/admin", include_in_schema=False)
    async def admin_page():
        # 禁用缓存：面板是单文件前端，缓存旧版会导致改完 bug 后仍看到旧行为
        return FileResponse(
            STATIC / "index.html",
            headers={"Cache-Control": "no-store, no-cache, must-revalidate",
                     "Pragma": "no-cache", "Expires": "0"},
        )

    @app.get("/health")
    async def health():
        accs = pool.list()
        return {"ok": True, "accounts": len(accs),
                "active": sum(1 for a in accs if a.available())}

    @app.on_event("shutdown")
    async def _shutdown():
        await service.aclose()      # 落盘调用记录
        await pool.close_all()

    @app.on_event("startup")
    async def _startup():
        from .model_catalog import set_models
        from .tabbit_client import TabbitError
        for region in ("cn", "intl"):
            for acc in pool.list(region):
                if acc.models:
                    set_models(region, acc.models)
                elif acc.token_cipher:
                    try:
                        models = await pool.client_for(acc).get_models()
                        set_models(region, models)
                        pool.refresh_from_upstream(acc.id, {}, {}, models)
                    except TabbitError:
                        pass
        # 定时把调用记录刷盘（不阻塞请求）
        async def _flusher():
            while True:
                await asyncio.sleep(3)
                try:
                    usage.flush()
                except Exception:
                    pass
        asyncio.create_task(_flusher())

    return app


def main() -> None:
    cfg = ConfigManager()
    host = cfg.get("server", "host", default="0.0.0.0")
    start_port = int(cfg.get("server", "port", default=8123))
    # 必须用与 uvicorn 相同的 host 探测：Windows 上占用 0.0.0.0:P 时
    # 绑定 127.0.0.1:P 仍可能成功，用 127.0.0.1 探测会漏判冲突。
    port = cfg.next_free_port(start_port, host)
    if port != start_port:
        warn(f"端口 {start_port} 被占用，改用 {port}")

    import uvicorn
    logger.info("=" * 56)
    logger.info(" Tabbit2API  管理面板: http://127.0.0.1:%d/admin", port)
    logger.info(" OpenAI 兼容: http://127.0.0.1:%d/v1/chat/completions", port)
    logger.info(" Claude 兼容: http://127.0.0.1:%d/v1/messages", port)
    logger.info("=" * 56)

    uvicorn.run(create_app(cfg), host=host, port=port, log_level="info",
                access_log=False)


if __name__ == "__main__":
    main()
