"""FastAPI application entrypoint."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.data_quality import router as data_quality_router
from app.api.market_monitor import router as market_monitor_router
from app.api.discovery import router as discovery_router
from app.api.health import router as system_router
from app.api.historical import router as historical_router
from app.api.point_in_time import router as point_in_time_router
from app.api.readiness import router as readiness_router
from app.api.quality_historical import router as quality_historical_router
from app.api.intelligence import router as intelligence_router
from app.api.opportunities import router as opportunities_router
from app.api.scheduler import router as scheduler_router
from app.api.creator import router as creator_router
from app.api.security import router as security_router
from app.api.tokens import router as tokens_router
from app.api.paper import router as paper_router
from app.api.paper_auto_entry import router as paper_auto_entry_router
from app.api.paper_auto_exit import router as paper_auto_exit_router
from app.config import get_settings
from app.database import get_session_factory, init_db
from app.services.discovery_scheduler import get_scheduler
from app.services.paper_entry_scheduler import get_entry_scheduler
from app.services.paper_exit_scheduler import get_exit_scheduler

logger = logging.getLogger("meme_hunter")


def configure_logging() -> None:
    if logging.getLogger().handlers:
        return
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    if not settings.paper_trading_enabled:
        raise RuntimeError(
            "Live trading is not supported. Set PAPER_TRADING_ENABLED=true."
        )
    if settings.paper_auto_trading:
        logger.warning(
            "PAPER_AUTO_TRADING is true, but live or unsupervised trading is not implemented. "
            "Paper auto-entry is controlled by PAPER_AUTO_ENTRY_ENABLED (default false)."
        )

    init_db()
    from app.services.paper_trading import ensure_default_portfolio

    paper_session = get_session_factory()()
    try:
        ensure_default_portfolio(paper_session, settings)
    finally:
        paper_session.close()
    scheduler = get_scheduler()
    entry_scheduler = get_entry_scheduler()
    exit_scheduler = get_exit_scheduler()
    await scheduler.start(settings)
    await entry_scheduler.start(settings)
    await exit_scheduler.start(settings)
    logger.info(
        "Started %s v%s env=%s paper_balance=%s auto_discovery=%s paper_auto_entry=%s paper_auto_exit=%s",
        settings.app_name,
        settings.app_version,
        settings.app_env,
        settings.paper_starting_balance,
        settings.auto_discovery_enabled,
        settings.paper_auto_entry_enabled,
        settings.paper_auto_exit_enabled,
    )
    try:
        yield
    finally:
        await exit_scheduler.stop()
        await entry_scheduler.stop()
        await scheduler.stop()
        from app.services.rpc_session import shutdown_shared_rpc_session

        await shutdown_shared_rpc_session()


def create_app() -> FastAPI:
    configure_logging()
    settings = get_settings()
    application = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description=(
            "Solana memecoin discovery platform. Paper trading only. "
            "No live wallets or private keys."
        ),
        lifespan=lifespan,
    )
    application.include_router(system_router)
    application.include_router(scheduler_router)
    application.include_router(discovery_router)
    application.include_router(tokens_router)
    application.include_router(intelligence_router)
    application.include_router(security_router)
    application.include_router(creator_router)
    application.include_router(opportunities_router)
    application.include_router(historical_router)
    application.include_router(quality_historical_router)
    application.include_router(point_in_time_router)
    application.include_router(data_quality_router)
    application.include_router(market_monitor_router)
    application.include_router(readiness_router)
    application.include_router(paper_router)
    application.include_router(paper_auto_entry_router)
    application.include_router(paper_auto_exit_router)

    @application.exception_handler(Exception)
    async def unhandled_exception_handler(_request, exc: Exception) -> JSONResponse:
        if isinstance(exc, (StarletteHTTPException, RequestValidationError)):
            raise exc
        logger.exception("Unhandled application error: %s", exc)
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error"},
        )

    return application


app = create_app()
