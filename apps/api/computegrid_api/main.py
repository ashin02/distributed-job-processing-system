"""FastAPI application entry point."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from computegrid_api.api.jobs import router as jobs_router
from computegrid_api.config import get_cors_settings


class HealthResponse(BaseModel):
    """Response returned by the liveness endpoint."""

    status: str


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
