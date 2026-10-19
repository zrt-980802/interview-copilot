"""Application factory for the v2 API."""

from fastapi import FastAPI

from .lifecycle import lifespan
from .settings import Settings


def create_app(settings: Settings) -> FastAPI:
    """Build an application without starting services or loading configuration."""
    app = FastAPI(title=settings.app_name, lifespan=lifespan)
    app.state.settings = settings
    return app
