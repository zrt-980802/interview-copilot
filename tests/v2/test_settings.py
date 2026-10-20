"""TDD tests for immutable v2 settings and explicit load_settings (Task 5).

Contract under test (spec 5.2 / task brief):

- ``AppSettings``, ``AudioSettings``, ``STTSettings``, ``LLMSettings`` and
  ``StorageSettings`` are frozen dataclasses.
- ``load_settings(path=None, env=None)`` is explicit: importing the module
  never loads configuration, reads files or touches the environment.
- Precedence: built-in defaults < config file < environment variables.
- Defaults are loopback-only (never 0.0.0.0), ports are valid, timeouts are
  non-negative and user paths are ``~``-expanded.
- No auto-scanning of Desktop, no reading of Hermes ``.env``, no network.
- Tests are hermetic: every case passes an explicit ``env`` mapping and a
  ``tmp_path`` config file; the developer's real ``config.json`` is never read.
"""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

from interview_copilot.settings import (
    AppSettings,
    AudioSettings,
    LLMSettings,
    STTSettings,
    StorageSettings,
    load_settings,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8527
DEFAULT_DATA_DIR_NAME = "InterviewCopilot"


def make_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    """A hermetic fake environment: empty APPDATA/LOCALAPPDATA, fake home."""
    env = {
        "APPDATA": str(tmp_path / "appdata"),
        "LOCALAPPDATA": str(tmp_path / "localappdata"),
        "USERPROFILE": str(tmp_path / "home"),
        "HOME": str(tmp_path / "home"),
    }
    env.update(extra)
    return env


def write_config(path: Path, config: dict) -> Path:
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def expected_default_data_dir(tmp_path: Path) -> Path:
    return tmp_path / "localappdata" / DEFAULT_DATA_DIR_NAME


# ---------------------------------------------------------------------------
# Missing config file
# ---------------------------------------------------------------------------


def test_missing_default_config_returns_pure_defaults(tmp_path: Path) -> None:
    settings = load_settings(env=make_env(tmp_path))

    assert isinstance(settings, AppSettings)
    assert settings.app_name == "Interview Copilot"
    assert settings.host == DEFAULT_HOST
    assert settings.port == DEFAULT_PORT

    # Windows-first data dir derived from the LOCALAPPDATA env lookup, which
    # load_settings performs only now (never at import).
    assert settings.storage.data_dir == expected_default_data_dir(tmp_path)

    assert isinstance(settings.audio, AudioSettings)
    assert settings.audio.sample_rate == 16000
    assert settings.audio.frame_ms == 100
    assert settings.audio.merge_wait_ms == 1100

    assert isinstance(settings.stt, STTSettings)
    assert settings.stt.model == "whisper-1"

    assert isinstance(settings.llm, LLMSettings)
    assert settings.llm.temperature == 0.6

    # Direct construction must not resolve the platform default either:
    # env lookup for defaults happens only inside load_settings.
    assert StorageSettings().data_dir is None
    assert AppSettings().storage.data_dir is None


def test_missing_explicit_config_file_raises(tmp_path: Path) -> None:
    missing = tmp_path / "missing" / "config.json"

    with pytest.raises(FileNotFoundError) as excinfo:
        load_settings(path=missing, env=make_env(tmp_path))

    assert os.fspath(missing) in str(excinfo.value)


# ---------------------------------------------------------------------------
# Local config file override
# ---------------------------------------------------------------------------


def test_local_config_file_overrides_defaults(tmp_path: Path) -> None:
    config = write_config(
        tmp_path / "config.json",
        {
            "port": 9000,
            "llm": {"base_url": "http://127.0.0.1:9999/v1"},
        },
    )

    settings = load_settings(path=config, env=make_env(tmp_path))

    assert settings.port == 9000
    assert settings.llm.base_url == "http://127.0.0.1:9999/v1"
    # Unspecified fields keep their defaults.
    assert settings.host == DEFAULT_HOST
    assert settings.audio.sample_rate == 16000
    assert settings.stt.model == "whisper-1"
    assert settings.storage.data_dir == expected_default_data_dir(tmp_path)


def test_legacy_config_shape_maps_all_shared_fields(tmp_path: Path) -> None:
    """The legacy config.json structure maps onto the five frozen dataclasses.

    Personal keys that v2 does not own yet (resume/papers/materials) must be
    tolerated and ignored. Only tmp_path fixtures are used here.
    """
    config = write_config(
        tmp_path / "config.json",
        {
            "host": "127.0.0.1",
            "port": 8527,
            "merge": {"wait_ms": 1100, "continuation_ms": 3500},
            "stt": {"url": "http://127.0.0.1:8760", "model": "large-v3-turbo"},
            "llm": {
                "base_url": "https://api.example.invalid/v1",
                "quick_model": "example-fast-model",
                "detail_model": "example-quality-model",
                "model": "example-default-model",
                "api_key_env": ["EXAMPLE_API_KEY"],
                "disable_thinking": True,
                "temperature": 0.6,
                "max_tokens": 800,
                "fallback_base_url": "https://fallback.example.invalid/v1",
                "fallback_model": "example-fallback-model",
                "fallback_api_key_env": ["EXAMPLE_FALLBACK_API_KEY"],
            },
            "audio": {
                "sample_rate": 16000,
                "frame_ms": 100,
                "silence_end_ms": 900,
                "min_segment_ms": 700,
                "max_segment_ms": 18000,
                "speech_threshold_offset_db": 11.0,
                "absolute_speech_db": -44.0,
                "merge_quiet_ms": 1600,
            },
            # Not owned by v2 settings yet; must be ignored, not rejected.
            "resume_path": "resume.txt",
            "paper_paths": ["paper.pdf"],
            "material_paths": ["material.pdf"],
        },
    )

    settings = load_settings(path=config, env=make_env(tmp_path))

    assert settings.host == "127.0.0.1"
    assert settings.port == 8527
    assert settings.audio.merge_wait_ms == 1100
    assert settings.audio.merge_continuation_ms == 3500
    assert settings.audio.merge_quiet_ms == 1600
    assert settings.audio.speech_threshold_offset_db == 11.0
    assert settings.audio.absolute_speech_db == -44.0
    assert settings.stt.url == "http://127.0.0.1:8760"
    assert settings.stt.model == "large-v3-turbo"
    assert settings.llm.base_url == "https://api.example.invalid/v1"
    assert settings.llm.quick_model == "example-fast-model"
    assert settings.llm.detail_model == "example-quality-model"
    assert settings.llm.model == "example-default-model"
    assert settings.llm.api_key_env == ("EXAMPLE_API_KEY",)
    assert settings.llm.disable_thinking is True
    assert settings.llm.temperature == 0.6
    assert settings.llm.max_tokens == 800
    assert settings.llm.fallback_base_url == "https://fallback.example.invalid/v1"
    assert settings.llm.fallback_model == "example-fallback-model"
    assert settings.llm.fallback_api_key_env == ("EXAMPLE_FALLBACK_API_KEY",)


# ---------------------------------------------------------------------------
# Environment variable override
# ---------------------------------------------------------------------------


def test_env_overrides_take_precedence_over_config_file(tmp_path: Path) -> None:
    config = write_config(
        tmp_path / "config.json",
        {
            "port": 9000,
            "llm": {"base_url": "https://file.example.invalid/v1"},
        },
    )
    env = make_env(
        tmp_path,
        IC_PORT="9100",
        IC_LLM_BASE_URL="https://env.example.invalid/v1",
    )

    settings = load_settings(path=config, env=env)

    assert settings.port == 9100
    assert settings.llm.base_url == "https://env.example.invalid/v1"


def test_env_overrides_apply_without_config_file(tmp_path: Path) -> None:
    env = make_env(
        tmp_path,
        IC_HOST="localhost",
        IC_PORT="8600",
        IC_DATA_DIR="~/ic-env-data",
        IC_STT_URL="http://127.0.0.1:8760",
        IC_STT_MODEL="env-stt-model",
        IC_LLM_BASE_URL="https://env.example.invalid/v1",
        IC_LLM_MODEL="env-model",
        IC_LLM_API_KEY_ENV="A_API_KEY, B_API_KEY",
    )

    settings = load_settings(env=env)

    assert settings.host == "localhost"
    assert settings.port == 8600
    assert settings.storage.data_dir == tmp_path / "home" / "ic-env-data"
    assert settings.stt.url == "http://127.0.0.1:8760"
    assert settings.stt.model == "env-stt-model"
    assert settings.llm.base_url == "https://env.example.invalid/v1"
    assert settings.llm.model == "env-model"
    assert settings.llm.api_key_env == ("A_API_KEY", "B_API_KEY")


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------


def test_settings_are_frozen(tmp_path: Path) -> None:
    settings = load_settings(env=make_env(tmp_path))

    with pytest.raises(dataclasses.FrozenInstanceError):
        settings.port = 1
    with pytest.raises(dataclasses.FrozenInstanceError):
        settings.audio.sample_rate = 2
    with pytest.raises(dataclasses.FrozenInstanceError):
        settings.llm.temperature = 0.9
    with pytest.raises(dataclasses.FrozenInstanceError):
        settings.storage.data_dir = tmp_path

    # Sequence settings are stored as immutable tuples, not lists.
    assert isinstance(settings.llm.api_key_env, tuple)
    assert isinstance(settings.stt.api_key_env, tuple)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_default_host_is_loopback_and_wildcard_host_is_rejected(tmp_path: Path) -> None:
    settings = load_settings(env=make_env(tmp_path))
    assert settings.host == DEFAULT_HOST

    wildcard_file = write_config(tmp_path / "wildcard.json", {"host": "0.0.0.0"})
    with pytest.raises(ValueError, match="0.0.0.0"):
        load_settings(path=wildcard_file, env=make_env(tmp_path))

    wildcard_env = make_env(tmp_path, IC_HOST="0.0.0.0")
    with pytest.raises(ValueError, match="0.0.0.0"):
        load_settings(env=wildcard_env)

    localhost_file = write_config(tmp_path / "localhost.json", {"host": "localhost"})
    assert load_settings(path=localhost_file, env=make_env(tmp_path)).host == "localhost"


@pytest.mark.parametrize("bad_port", [0, -1, 70000, True, "8527"])
def test_invalid_port_raises_clear_error(tmp_path: Path, bad_port) -> None:
    config = write_config(tmp_path / "config.json", {"port": bad_port})

    with pytest.raises(ValueError, match="port"):
        load_settings(path=config, env=make_env(tmp_path))


def test_invalid_env_port_raises_clear_error(tmp_path: Path) -> None:
    env = make_env(tmp_path, IC_PORT="not-a-number")

    with pytest.raises(ValueError, match="IC_PORT"):
        load_settings(env=env)


@pytest.mark.parametrize(
    ("section", "field_name", "bad_value"),
    [("stt", "timeout", -1), ("stt", "timeout", -0.5), ("llm", "timeout", -30)],
)
def test_negative_timeouts_raise(
    tmp_path: Path, section: str, field_name: str, bad_value
) -> None:
    config = write_config(tmp_path / "config.json", {section: {field_name: bad_value}})

    with pytest.raises(ValueError, match=field_name):
        load_settings(path=config, env=make_env(tmp_path))


def test_zero_timeout_is_allowed(tmp_path: Path) -> None:
    config = write_config(
        tmp_path / "config.json", {"stt": {"timeout": 0}, "llm": {"timeout": 0}}
    )

    settings = load_settings(path=config, env=make_env(tmp_path))

    assert settings.stt.timeout == 0
    assert settings.llm.timeout == 0


def test_user_paths_are_expanded(tmp_path: Path) -> None:
    config = write_config(
        tmp_path / "config.json",
        {"storage": {"data_dir": "~/ic-file-data"}},
    )
    env = make_env(tmp_path, IC_DATA_DIR="~/ic-env-data")

    settings = load_settings(path=config, env=env)

    # Env override wins and both expansions use the injected fake home.
    assert settings.storage.data_dir == tmp_path / "home" / "ic-env-data"

    file_only = load_settings(path=config, env=make_env(tmp_path))
    assert file_only.storage.data_dir == tmp_path / "home" / "ic-file-data"


@pytest.mark.parametrize(
    ("payload", "message_part"),
    [
        ('{"port": 8527', "config.json"),
        ("[1, 2, 3]", "JSON object"),
        ('{"stt": 5}', "stt"),
        ('{"audio": {"min_segment_ms": 2000, "max_segment_ms": 1000}}', "max_segment_ms"),
    ],
)
def test_invalid_config_values_raise_clear_errors(
    tmp_path: Path, payload: str, message_part: str
) -> None:
    config = tmp_path / "config.json"
    config.write_text(payload, encoding="utf-8")

    with pytest.raises(ValueError, match=message_part):
        load_settings(path=config, env=make_env(tmp_path))


# ---------------------------------------------------------------------------
# load_settings is never called at import time
# ---------------------------------------------------------------------------


def test_import_does_not_load_settings(tmp_path: Path) -> None:
    """Importing the package must not read any config file or env override."""
    probe = textwrap.dedent(
        """
        import sys
        from pathlib import Path

        sys.path.insert(0, sys.argv[1])

        blocked_reads = []

        def audit(event, args):
            if event in {"open", "os.open"} and args:
                path = str(args[0]).replace("\\\\", "/")
                name = path.rsplit("/", 1)[-1].lower()
                if "config.json" in name or name == ".env":
                    blocked_reads.append(path)
                    raise AssertionError(f"config file read during import: {path}")

        sys.addaudithook(audit)

        import interview_copilot
        import interview_copilot.settings

        # A poisoned env var would surface here if import consulted it.
        settings = interview_copilot.Settings()
        assert settings.port == 8527, settings.port
        assert settings.host == "127.0.0.1", settings.host
        assert callable(interview_copilot.settings.load_settings)
        assert blocked_reads == []
        print("import-clean")
        """
    )
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    # Sentinel: if import machinery ever resolved a cwd config.json, the audit
    # hook would abort the subprocess.
    (cwd / "config.json").write_text('{"port": 65432}', encoding="utf-8")
    env = {
        **os.environ,
        "APPDATA": str(tmp_path / "appdata"),
        "LOCALAPPDATA": str(tmp_path / "localappdata"),
        "IC_PORT": "59999",
        "IC_HOST": "0.0.0.0",
    }

    result = subprocess.run(
        [sys.executable, "-I", "-c", probe, str(SRC_ROOT)],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert "import-clean" in result.stdout
