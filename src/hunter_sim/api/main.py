"""FastAPI 应用主入口（PROMPT-API-001）。

HunterSim REST API 服务，基础路径 /api/v1/sim。
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import ORJSONResponse

from hunter_sim.api.deps import set_app_settings
from hunter_sim.api.error_handlers import register_exception_handlers
from hunter_sim.api.middleware.auth import JWTAuthMiddleware
from hunter_sim.api.middleware.logging import RequestLogMiddleware
from hunter_sim.api.routers import health, instances, resources, scenarios, scenes, websocket
from hunter_sim.common.models import HunterSimSettings
from hunter_sim.common.utils import get_logger
from hunter_sim.resource_manager.health_monitor import ResourceQuotaManager

logger = get_logger(__name__)


def create_app(settings: HunterSimSettings | None = None) -> FastAPI:
    """创建并配置 FastAPI 应用实例。

    Args:
        settings: HunterSim 全局配置，None 时使用环境变量默认值。

    Returns:
        配置完成的 FastAPI 应用。
    """
    if settings is None:
        settings = HunterSimSettings()

    # 注入全局配置到 deps 模块
    set_app_settings(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logger.info(f"HunterSim API starting (env={settings.env}, port={settings.api.port})")
        yield
        logger.info("HunterSim API shutting down")

    app = FastAPI(
        title="HunterSim API",
        version="2.1.1",
        description="HUNTER SE VIL 仿真平台 REST API",
        default_response_class=ORJSONResponse,
        lifespan=lifespan,
        docs_url="/api/v1/sim/docs",
        redoc_url="/api/v1/sim/redoc",
        openapi_url="/api/v1/sim/openapi.json",
    )

    # ── 中间件（顺序：后添加的先执行）────────────────────────────────────────
    app.add_middleware(RequestLogMiddleware)
    app.add_middleware(
        JWTAuthMiddleware,
        api_settings=settings.api,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.api.cors_allow_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── 异常处理 ──────────────────────────────────────────────────────────────
    register_exception_handlers(app)

    # 用户级并发配额（设计文档 §14.2：单用户默认 5 个实例）
    app.state.quota_manager = ResourceQuotaManager(
        max_per_user=settings.resource.max_instances_per_user,
        max_total=settings.resource.max_instances_total,
    )

    # ── 路由注册 ──────────────────────────────────────────────────────────────
    api_prefix = "/api/v1/sim"
    app.include_router(health.router, prefix=api_prefix, tags=["health"])
    app.include_router(instances.router, prefix=f"{api_prefix}/instances", tags=["instances"])
    app.include_router(scenes.router, prefix=f"{api_prefix}/scenes", tags=["scenes"])
    app.include_router(scenarios.router, prefix=f"{api_prefix}/scenarios", tags=["scenarios"])
    app.include_router(resources.router, prefix=api_prefix, tags=["resources"])
    app.include_router(websocket.router, prefix=api_prefix, tags=["websocket"])

    logger.info("HunterSim FastAPI application created")
    return app


# 应用实例（供 uvicorn 直接引用）
app = create_app()
