"""Explicit lifecycle boundary for future runtime resource assembly."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI


@asynccontextmanager
async def lifespan(app: "FastAPI") -> AsyncIterator[None]:
    """Run the empty v2 lifecycle until resources are added explicitly."""
    del app
    yield
