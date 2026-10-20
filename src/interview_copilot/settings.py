"""Immutable settings and explicit configuration loading for Interview Copilot v2.

Design rules (spec 5.2):

- Importing this module performs no I/O: no config file is read, no
  environment variable is consulted, and ``load_settings`` is never invoked
  implicitly. The v2 FastAPI lifespan calls ``load_settings`` at startup.
- ``load_settings(path=None, env=None)`` is the single entry point that reads
  a JSON config file and environment overrides. Precedence: built-in
  defaults < config file < ``IC_*`` environment variables.
- Settings objects are frozen dataclasses; sequence fields are tuples.
- Defaults are loopback-only (never ``0.0.0.0``) so a misconfiguration can
  never expose the copilot beyond the local machine.
- Only *names* of API-key environment variables are stored; secrets are
  resolved later by provider clients. This module never reads a Hermes
  ``.env`` file, never scans Desktop or other user folders, and never
  touches the network.

Supported environment overrides (consulted only inside ``load_settings``):

- ``IC_HOST``, ``IC_PORT``
- ``IC_DATA_DIR``
- ``IC_STT_URL``, ``IC_STT_MODEL``
- ``IC_LLM_BASE_URL``, ``IC_LLM_MODEL``, ``IC_LLM_API_KEY_ENV`` (comma-separated names)

Default locations (Windows-first, resolved from the injected ``env`` mapping
only while ``load_settings`` runs):

- config file: ``%APPDATA%/InterviewCopilot/config.json`` (fallback:
  ``$XDG_CONFIG_HOME`` or ``~/.config``)
- storage/data directory: ``%LOCALAPPDATA%/InterviewCopilot`` (fallback:
  ``$XDG_DATA_HOME`` or ``~/.local/share``)

An explicitly provided ``path`` that does not exist raises
``FileNotFoundError``; the default config file is optional (a missing default
file simply yields defaults plus environment overrides).
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "AppSettings",
    "AudioSettings",
    "LLMSettings",
    "STTSettings",
    "Settings",
    "StorageSettings",
    "load_settings",
]

DEFAULT_APP_NAME = "Interview Copilot"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8527
DEFAULT_STT_MODEL = "whisper-1"
DATA_DIR_NAME = "InterviewCopilot"
CONFIG_FILE_NAME = "config.json"


# ---------------------------------------------------------------------------
# Validation helpers (pure; no I/O, no environment access)
# ---------------------------------------------------------------------------


def _type_name(value: Any) -> str:
    return type(value).__name__


def _checked_int(value: Any, label: str, *, minimum: int | None = None, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            f"{label} must be an integer, got {value!r} ({_type_name(value)})"
        )
    if minimum is not None and value < minimum:
        raise ValueError(f"{label} must be >= {minimum}, got {value}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{label} must be <= {maximum}, got {value}")
    return value


def _checked_float(
    value: Any,
    label: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(
            f"{label} must be a number, got {value!r} ({_type_name(value)})"
        )
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} must be a finite number, got {value!r}")
    if minimum is not None and number < minimum:
        raise ValueError(f"{label} must be >= {minimum}, got {number}")
    if maximum is not None and number > maximum:
        raise ValueError(f"{label} must be <= {maximum}, got {number}")
    return number


def _checked_str(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(
            f"{label} must be a string, got {value!r} ({_type_name(value)})"
        )
    return value


def _checked_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be true or false, got {value!r}")
    return value


def _checked_http_url(value: Any, label: str) -> str:
    text = _checked_str(value, label)
    if text and not text.lower().startswith(("http://", "https://")):
        raise ValueError(
            f"{label} must start with http:// or https://, got {text!r}"
        )
    return text


def _checked_env_names(value: Any, label: str) -> tuple[str, ...]:
    """Normalize a list of environment-variable *names* into a tuple."""
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ValueError(
            f"{label} must be a list of environment variable names, got {value!r}"
        )
    names: list[str] = []
    for item in value:
        name = _checked_str(item, f"{label} entry").strip()
        if not name:
            raise ValueError(
                f"{label} must not contain empty environment variable names"
            )
        names.append(name)
    return tuple(names)


# ---------------------------------------------------------------------------
# Frozen settings dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AudioSettings:
    """Audio capture and segmentation parameters (ms fields are milliseconds)."""

    sample_rate: int = 16000
    frame_ms: int = 100
    silence_end_ms: int = 900
    min_segment_ms: int = 700
    max_segment_ms: int = 18000
    speech_threshold_offset_db: float = 11.0
    absolute_speech_db: float = -44.0
    merge_quiet_ms: int = 1600
    # Legacy ``merge`` section: question merging debounce timings.
    merge_wait_ms: int = 1100
    merge_continuation_ms: int = 0

    def __post_init__(self) -> None:
        _checked_int(self.sample_rate, "audio.sample_rate", minimum=1)
        _checked_int(self.frame_ms, "audio.frame_ms", minimum=1)
        _checked_int(self.silence_end_ms, "audio.silence_end_ms", minimum=0)
        _checked_int(self.min_segment_ms, "audio.min_segment_ms", minimum=0)
        _checked_int(self.max_segment_ms, "audio.max_segment_ms", minimum=0)
        _checked_int(self.merge_quiet_ms, "audio.merge_quiet_ms", minimum=0)
        _checked_int(self.merge_wait_ms, "audio.merge_wait_ms", minimum=0)
        _checked_int(self.merge_continuation_ms, "audio.merge_continuation_ms", minimum=0)
        _checked_float(self.speech_threshold_offset_db, "audio.speech_threshold_offset_db")
        _checked_float(self.absolute_speech_db, "audio.absolute_speech_db")
        if self.min_segment_ms > self.max_segment_ms:
            raise ValueError(
                "audio.min_segment_ms must be <= audio.max_segment_ms, "
                f"got min {self.min_segment_ms} > max {self.max_segment_ms}"
            )


@dataclass(frozen=True, slots=True)
class STTSettings:
    """OpenAI-compatible speech-to-text endpoint (local faster-whisper or remote)."""

    url: str = ""
    model: str = DEFAULT_STT_MODEL
    api_key_env: tuple[str, ...] = ()
    timeout: float = 30.0

    def __post_init__(self) -> None:
        _checked_http_url(self.url, "stt.url")
        _checked_str(self.model, "stt.model")
        object.__setattr__(
            self, "api_key_env", _checked_env_names(self.api_key_env, "stt.api_key_env")
        )
        _checked_float(self.timeout, "stt.timeout", minimum=0.0)


@dataclass(frozen=True, slots=True)
class LLMSettings:
    """LLM completion settings shared by answering and knowledge flows."""

    base_url: str = ""
    quick_model: str = ""
    detail_model: str = ""
    model: str = ""
    api_key_env: tuple[str, ...] = ()
    disable_thinking: bool = True
    temperature: float = 0.6
    max_tokens: int = 800
    fallback_base_url: str = ""
    fallback_model: str = ""
    fallback_api_key_env: tuple[str, ...] = ()
    timeout: float = 60.0

    def __post_init__(self) -> None:
        _checked_http_url(self.base_url, "llm.base_url")
        _checked_str(self.quick_model, "llm.quick_model")
        _checked_str(self.detail_model, "llm.detail_model")
        _checked_str(self.model, "llm.model")
        object.__setattr__(
            self, "api_key_env", _checked_env_names(self.api_key_env, "llm.api_key_env")
        )
        _checked_bool(self.disable_thinking, "llm.disable_thinking")
        _checked_float(self.temperature, "llm.temperature", minimum=0.0, maximum=2.0)
        _checked_int(self.max_tokens, "llm.max_tokens", minimum=1)
        _checked_http_url(self.fallback_base_url, "llm.fallback_base_url")
        _checked_str(self.fallback_model, "llm.fallback_model")
        object.__setattr__(
            self,
            "fallback_api_key_env",
            _checked_env_names(self.fallback_api_key_env, "llm.fallback_api_key_env"),
        )
        _checked_float(self.timeout, "llm.timeout", minimum=0.0)


@dataclass(frozen=True, slots=True)
class StorageSettings:
    """User-data directory for history and runtime storage (spec 5.8).

    ``data_dir`` is ``None`` until ``load_settings`` resolves it: direct
    construction deliberately performs no environment lookup. The resolved
    default is ``%LOCALAPPDATA%/InterviewCopilot`` (Windows-first).
    """

    data_dir: Path | None = None

    def __post_init__(self) -> None:
        if self.data_dir is None:
            return
        if isinstance(self.data_dir, (str, os.PathLike)):
            object.__setattr__(self, "data_dir", Path(self.data_dir))
        else:
            raise ValueError(
                "storage.data_dir must be a path or None, "
                f"got {self.data_dir!r} ({_type_name(self.data_dir)})"
            )


@dataclass(frozen=True, slots=True)
class AppSettings:
    """Top-level immutable settings handed to ``create_app`` explicitly."""

    app_name: str = DEFAULT_APP_NAME
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    audio: AudioSettings = field(default_factory=AudioSettings)
    stt: STTSettings = field(default_factory=STTSettings)
    llm: LLMSettings = field(default_factory=LLMSettings)
    storage: StorageSettings = field(default_factory=StorageSettings)

    def __post_init__(self) -> None:
        name = _checked_str(self.app_name, "app_name")
        if not name.strip():
            raise ValueError("app_name must not be empty")
        host = _checked_str(self.host, "host")
        if not host.strip():
            raise ValueError("host must not be empty")
        if host.strip().lower() in {"0.0.0.0", "::", "[::]"}:
            raise ValueError(
                "host must be a loopback address such as 127.0.0.1 or localhost; "
                f"refusing {host!r} because it would expose the copilot beyond "
                "the local machine"
            )
        _checked_int(self.port, "port", minimum=1, maximum=65535)
        for attribute, expected in (
            ("audio", AudioSettings),
            ("stt", STTSettings),
            ("llm", LLMSettings),
            ("storage", StorageSettings),
        ):
            actual = getattr(self, attribute)
            if not isinstance(actual, expected):
                raise ValueError(
                    f"{attribute} must be {expected.__name__}, got {_type_name(actual)}"
                )


#: Backwards-compatible public name (Task 4 contract): ``Settings`` stays
#: importable and default-constructible from the package root and carries
#: ``app_name`` for the application factory. It is now the full AppSettings.
Settings = AppSettings


# ---------------------------------------------------------------------------
# Explicit loading (the only place with file or environment access)
# ---------------------------------------------------------------------------


def _env_value(env_map: Mapping[str, Any], name: str) -> str | None:
    value = env_map.get(name)
    if value is None:
        return None
    text = str(value)
    return text if text.strip() else None


def _env_int(text: str, name: str) -> int:
    try:
        return int(text.strip())
    except ValueError:
        raise ValueError(
            f"environment variable {name} must be an integer, got {text!r}"
        ) from None


def _int_field(
    section: Mapping[str, Any],
    key: str,
    default: int,
    label: str,
    *,
    minimum: int | None = None,
) -> int:
    return _checked_int(section.get(key, default), label, minimum=minimum)


def _float_field(
    section: Mapping[str, Any],
    key: str,
    default: float,
    label: str,
    *,
    minimum: float | None = None,
) -> float:
    return _checked_float(section.get(key, default), label, minimum=minimum)


def _bool_field(
    section: Mapping[str, Any], key: str, default: bool, label: str
) -> bool:
    return _checked_bool(section.get(key, default), label)


def _section(raw: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = raw.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(
            f"config section '{key}' must be an object, got {_type_name(value)}"
        )
    return value


def _home_dir(env_map: Mapping[str, str]) -> Path:
    home = env_map.get("USERPROFILE") or env_map.get("HOME")
    if not home:
        raise ValueError(
            "cannot expand '~': neither USERPROFILE nor HOME is set in the "
            "provided environment"
        )
    return Path(str(home))


def _expand_path(value: str | os.PathLike[str], env_map: Mapping[str, str]) -> Path:
    """Expand a leading ``~`` using only the injected ``env`` mapping."""
    text = os.fspath(value)
    if text == "~":
        return _home_dir(env_map)
    if text.startswith("~/") or text.startswith("~\\"):
        rest = text[2:]
        home = _home_dir(env_map)
        return home / rest if rest else home
    if text.startswith("~"):
        raise ValueError(
            f"only '~' and '~/...' paths are supported, got {text!r}"
        )
    return Path(text)


def _default_data_dir(env_map: Mapping[str, str]) -> Path:
    """Windows-first user data dir: %LOCALAPPDATA%/InterviewCopilot."""
    local_app_data = env_map.get("LOCALAPPDATA")
    if local_app_data:
        return Path(str(local_app_data)) / DATA_DIR_NAME
    xdg_data_home = env_map.get("XDG_DATA_HOME")
    if xdg_data_home:
        return Path(str(xdg_data_home)) / DATA_DIR_NAME
    return _expand_path("~/.local/share", env_map) / DATA_DIR_NAME


def _default_config_path(env_map: Mapping[str, str]) -> Path:
    app_data = env_map.get("APPDATA")
    if app_data:
        return Path(str(app_data)) / DATA_DIR_NAME / CONFIG_FILE_NAME
    xdg_config_home = env_map.get("XDG_CONFIG_HOME")
    if xdg_config_home:
        return Path(str(xdg_config_home)) / DATA_DIR_NAME / CONFIG_FILE_NAME
    return _expand_path("~/.config", env_map) / DATA_DIR_NAME / CONFIG_FILE_NAME


def _read_config_file(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise FileNotFoundError(
            f"settings file not found: {os.fspath(path)}"
        ) from None
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{os.fspath(path)} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(
            f"{os.fspath(path)} must contain a JSON object, got {_type_name(raw)}"
        )
    return raw


def _resolve_data_dir(storage_raw: Mapping[str, Any], env_map: Mapping[str, str]) -> Path:
    override = _env_value(env_map, "IC_DATA_DIR")
    if override is not None:
        return _expand_path(override, env_map)
    configured = storage_raw.get("data_dir")
    if configured is None:
        return _default_data_dir(env_map)
    return _expand_path(_checked_str(configured, "storage.data_dir"), env_map)


def _build_settings(raw: Mapping[str, Any], env_map: Mapping[str, str]) -> AppSettings:
    audio_raw = _section(raw, "audio")
    merge_raw = _section(raw, "merge")
    stt_raw = _section(raw, "stt")
    llm_raw = _section(raw, "llm")
    storage_raw = _section(raw, "storage")

    host = _env_value(env_map, "IC_HOST")
    if host is None:
        host = raw.get("host", DEFAULT_HOST)
    port_text = _env_value(env_map, "IC_PORT")
    if port_text is None:
        port = _checked_int(raw.get("port", DEFAULT_PORT), "port", minimum=1, maximum=65535)
    else:
        port = _checked_int(
            _env_int(port_text, "IC_PORT"), "port", minimum=1, maximum=65535
        )

    stt_url = _env_value(env_map, "IC_STT_URL")
    if stt_url is None:
        stt_url = stt_raw.get("url", "")
    stt_model = _env_value(env_map, "IC_STT_MODEL")
    if stt_model is None:
        stt_model = stt_raw.get("model", DEFAULT_STT_MODEL)

    llm_base_url = _env_value(env_map, "IC_LLM_BASE_URL")
    if llm_base_url is None:
        llm_base_url = llm_raw.get("base_url", "")
    llm_model = _env_value(env_map, "IC_LLM_MODEL")
    if llm_model is None:
        llm_model = llm_raw.get("model", "")
    llm_key_env_text = _env_value(env_map, "IC_LLM_API_KEY_ENV")
    if llm_key_env_text is None:
        llm_api_key_env: Sequence[str] = llm_raw.get("api_key_env", [])
    else:
        llm_api_key_env = [
            name.strip() for name in llm_key_env_text.split(",") if name.strip()
        ]

    return AppSettings(
        app_name=DEFAULT_APP_NAME,
        host=host,
        port=port,
        audio=AudioSettings(
            sample_rate=_int_field(audio_raw, "sample_rate", 16000, "audio.sample_rate", minimum=1),
            frame_ms=_int_field(audio_raw, "frame_ms", 100, "audio.frame_ms", minimum=1),
            silence_end_ms=_int_field(audio_raw, "silence_end_ms", 900, "audio.silence_end_ms", minimum=0),
            min_segment_ms=_int_field(audio_raw, "min_segment_ms", 700, "audio.min_segment_ms", minimum=0),
            max_segment_ms=_int_field(audio_raw, "max_segment_ms", 18000, "audio.max_segment_ms", minimum=0),
            speech_threshold_offset_db=_float_field(
                audio_raw, "speech_threshold_offset_db", 11.0, "audio.speech_threshold_offset_db"
            ),
            absolute_speech_db=_float_field(
                audio_raw, "absolute_speech_db", -44.0, "audio.absolute_speech_db"
            ),
            merge_quiet_ms=_int_field(audio_raw, "merge_quiet_ms", 1600, "audio.merge_quiet_ms", minimum=0),
            merge_wait_ms=_int_field(merge_raw, "wait_ms", 1100, "merge.wait_ms", minimum=0),
            merge_continuation_ms=_int_field(
                merge_raw, "continuation_ms", 0, "merge.continuation_ms", minimum=0
            ),
        ),
        stt=STTSettings(
            url=stt_url,
            model=stt_model,
            api_key_env=stt_raw.get("api_key_env", []),
            timeout=_float_field(stt_raw, "timeout", 30.0, "stt.timeout", minimum=0.0),
        ),
        llm=LLMSettings(
            base_url=llm_base_url,
            quick_model=_checked_str(llm_raw.get("quick_model", ""), "llm.quick_model"),
            detail_model=_checked_str(llm_raw.get("detail_model", ""), "llm.detail_model"),
            model=llm_model,
            api_key_env=llm_api_key_env,
            disable_thinking=_bool_field(llm_raw, "disable_thinking", True, "llm.disable_thinking"),
            temperature=_float_field(llm_raw, "temperature", 0.6, "llm.temperature"),
            max_tokens=_int_field(llm_raw, "max_tokens", 800, "llm.max_tokens", minimum=1),
            fallback_base_url=_checked_str(llm_raw.get("fallback_base_url", ""), "llm.fallback_base_url"),
            fallback_model=_checked_str(llm_raw.get("fallback_model", ""), "llm.fallback_model"),
            fallback_api_key_env=llm_raw.get("fallback_api_key_env", []),
            timeout=_float_field(llm_raw, "timeout", 60.0, "llm.timeout", minimum=0.0),
        ),
        storage=StorageSettings(data_dir=_resolve_data_dir(storage_raw, env_map)),
    )


def load_settings(
    path: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> AppSettings:
    """Load, validate and freeze application settings.

    ``path`` selects the JSON config file. When omitted, the Windows-first
    default location is ``%APPDATA%/InterviewCopilot/config.json`` and a
    missing default file simply yields defaults. An explicitly provided
    missing path raises ``FileNotFoundError``.

    ``env`` is the environment mapping consulted for defaults and ``IC_*``
    overrides; it defaults to ``os.environ`` and is only read inside this
    function — never at import time.

    Precedence: built-in defaults < config file < environment variables.
    """
    env_map: Mapping[str, str] = os.environ if env is None else env
    if path is None:
        config_path = _default_config_path(env_map)
        raw: dict[str, Any] = (
            _read_config_file(config_path) if config_path.is_file() else {}
        )
    else:
        config_path = Path(path)
        raw = _read_config_file(config_path)

    try:
        return _build_settings(raw, env_map)
    except ValueError as exc:
        source = os.fspath(config_path) if path is not None else "defaults and environment"
        raise ValueError(f"invalid settings (from {source}): {exc}") from exc
