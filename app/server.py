import logging
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app import db as database
from app.config import Settings, get_settings
from app.postings.routes import router as postings_router

logger = logging.getLogger(__name__)


class HealthResponse(BaseModel):
    """`GET /health`: whether the service can serve requests, and whether MongoDB answers."""

    status: Literal["ok", "unavailable"]
    mongodb: Literal["ok", "unreachable"]


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the API. Connects to MongoDB and ensures the indexes on start."""
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if not settings.api_keys:
            logger.warning("No API keys configured: every request to the job-postings endpoints will get 401")
        client = database.create_client(settings)
        app.state.settings = settings
        app.state.db = client[settings.mongodb_database_name]
        try:
            # Fails the start when MongoDB is unreachable, so the container restarts instead of serving errors.
            await database.ensure_indexes(app.state.db)
            yield
        finally:
            await client.close()

    app = FastAPI(title="job-postings", lifespan=lifespan)
    if settings.cors_allow_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_allow_origins,
            allow_methods=["GET", "POST"],
            allow_headers=["content-type", "x-api-key"],
        )
    app.include_router(postings_router)

    @app.get("/health", response_model=HealthResponse)
    async def health(request: Request, response: Response) -> HealthResponse:
        if await database.ping(request.app.state.db):
            return HealthResponse(status="ok", mongodb="ok")
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return HealthResponse(status="unavailable", mongodb="unreachable")

    return app
