"""FastAPI application entry point."""

import logging
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from computegrid_api.api.jobs import router as jobs_router
from computegrid_api.config import get_cors_settings
from computegrid_api.db.session import SessionFactory, get_session_factory
from computegrid_api.logging_config import configure_logging

configure_logging("api")
logger = logging.getLogger(__name__)


class HealthResponse(BaseModel):
    """Response returned by the liveness endpoint."""

    status: str


class ReadinessResponse(BaseModel):
    """Response returned when essential API dependencies are available."""

    status: str
    database: str


app = FastAPI(title="ComputeGrid API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_cors_settings().cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
)
app.include_router(jobs_router)


@app.get("/healthz", response_model=HealthResponse, tags=["health"])
async def healthz() -> HealthResponse:
    """Report that the API process is responsive."""
    return HealthResponse(status="ok")


@app.get(
    "/readyz",
    response_model=ReadinessResponse,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "PostgreSQL unavailable"}},
    tags=["health"],
)
def readyz(
    session_factory: Annotated[SessionFactory, Depends(get_session_factory)],
) -> ReadinessResponse:
    """Report whether the API can reach its essential PostgreSQL dependency."""
    try:
        with session_factory() as session:
            session.execute(text("SELECT 1"))
    except SQLAlchemyError as error:
        logger.warning(
            "PostgreSQL readiness check failed",
            extra={"event": "api.readiness_failed"},
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="PostgreSQL is unavailable",
        ) from error
    return ReadinessResponse(status="ready", database="ok")
