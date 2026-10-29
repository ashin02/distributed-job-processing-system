"""FastAPI application entry point."""

from fastapi import FastAPI
from pydantic import BaseModel


class HealthResponse(BaseModel):
    """Response returned by the liveness endpoint."""

    status: str


app = FastAPI(title="ComputeGrid API")


@app.get("/healthz", response_model=HealthResponse, tags=["health"])
async def healthz() -> HealthResponse:
    """Report that the API process is responsive."""
    return HealthResponse(status="ok")

