"""Side-effect-free public entry points for Interview Copilot v2."""

from .app import create_app
from .settings import Settings, load_settings

__all__ = ["Settings", "create_app", "load_settings"]
