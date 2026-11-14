"""System health and status endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database import check_database_connection, get_db
from app.schemas import (
    ApplicationStatus,
    DatabaseStatus,
    HealthResponse,
    PaperTradingStatus,
    RootResponse,
    SystemStatusResponse,
)

router = APIRouter(tags=["system"])


def _database_backend(settings: Settings) -> str:
    if settings.sqlite_backend:
        return "sqlite"
    return "unknown"


@router.get("/", response_model=RootResponse)
def root(settings: Settings = Depends(get_settings)) -> RootResponse:
    return RootResponse(name=settings.app_name, version=settings.app_version)


@router.get("/health", response_model=HealthResponse)
def health(
    response: Response,
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> HealthResponse:
    connected = check_database_connection(db)
    if not connected:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        app_status = "unhealthy"
    else:
        app_status = "ok"

    return HealthResponse(
        status=app_status,
        environment=settings.app_env,
        database_connected=connected,
        paper_trading_enabled=settings.paper_trading_enabled,
        paper_auto_trading=settings.paper_auto_trading,
        paper_starting_balance=settings.paper_starting_balance,
    )


@router.get("/api/v1/status", response_model=SystemStatusResponse)
def system_status(
    response: Response,
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> SystemStatusResponse:
    connected = check_database_connection(db)
    app_status = "ok" if connected else "unhealthy"
    if not connected:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return SystemStatusResponse(
        application=ApplicationStatus(
            name=settings.app_name,
            version=settings.app_version,
            environment=settings.app_env,
            status=app_status,
        ),
        database=DatabaseStatus(
            connected=connected,
            backend=_database_backend(settings),
        ),
        paper_trading=PaperTradingStatus(
            enabled=settings.paper_trading_enabled,
            auto_trading=settings.paper_auto_trading,
            starting_balance=settings.paper_starting_balance,
            live_trading=False,
            mode="paper",
        ),
    )
