"""Minimal immutable settings boundary for the v2 skeleton."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Settings:
    """Settings passed explicitly to the application factory."""

    app_name: str = "Interview Copilot"
