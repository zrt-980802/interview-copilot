from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import textwrap


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"


def test_import_and_factory_are_side_effect_free(tmp_path: Path) -> None:
    probe = textwrap.dedent(
        """
        import _thread
        import builtins
        import json
        import os
        from pathlib import Path
        import socket
        import sys
        import threading

        sys.path.insert(0, sys.argv[1])
        workdir = Path.cwd()
        sandbox = workdir.parent
        fake_home = sandbox / "home"
        fake_userprofile = sandbox / "userprofile"
        fake_localappdata = sandbox / "localappdata"
        fake_appdata = sandbox / "appdata"
        runtime_root = fake_localappdata / "InterviewCopilot"
        repo_root = Path(sys.argv[2])
        monitored_roots = (
            workdir,
            fake_home,
            fake_userprofile,
            fake_localappdata,
            fake_appdata,
            runtime_root,
            repo_root / "data",
            repo_root / "config.json",
            repo_root / ".env",
        )
        for name, value in {
            "HOME": fake_home,
            "USERPROFILE": fake_userprofile,
            "LOCALAPPDATA": fake_localappdata,
            "APPDATA": fake_appdata,
            "XDG_CONFIG_HOME": fake_appdata,
            "XDG_DATA_HOME": fake_localappdata,
        }.items():
            os.environ[name] = os.fspath(value)
        sys.dont_write_bytecode = True

        def snapshot():
            result = {}
            for root in monitored_roots:
                if root.is_dir():
                    result[os.fspath(root)] = sorted(
                        os.fspath(path.relative_to(root))
                        for path in root.rglob("*")
                    )
                elif root.exists():
                    result[os.fspath(root)] = ["<file>"]
            return result

        before_entries = snapshot()
        before_threads = {thread.ident for thread in threading.enumerate()}
        thread_starts = []
        network_calls = []
        protected_io = []
        write_attempts = []
        audit_active = True

        def path_value(value):
            if isinstance(value, (str, bytes, os.PathLike)):
                return os.fsdecode(os.fspath(value))
            return None

        def is_protected(path):
            value = path_value(path)
            if value is None:
                return False
            candidate = Path(value).absolute()
            return any(
                candidate == root or root in candidate.parents
                for root in monitored_roots
            )

        def reject_network(label):
            network_calls.append(label)
            raise AssertionError(f"network operation attempted: {label}")

        def reject_thread_start(thread, *args, **kwargs):
            thread_starts.append(f"threading:{thread.name}")
            raise AssertionError("thread started during import/factory construction")

        def reject_low_level_thread(*args, **kwargs):
            thread_starts.append("_thread.start_new_thread")
            raise AssertionError("low-level thread started during import/factory construction")

        def audit(event, args):
            if event.startswith("socket.") or event in {
                "urllib.Request",
                "socket.getaddrinfo",
                "socket.gethostbyname",
            }:
                network_calls.append(event)
                raise AssertionError(f"network audit event: {event}")
            if not audit_active:
                return
            if event == "open" and len(args) >= 2:
                path, mode = args[0], args[1]
                if any(flag in mode for flag in ("w", "a", "x", "+")):
                    write_attempts.append(f"open:{path_value(path)}")
                    raise AssertionError("file write attempted during import/factory construction")
                if is_protected(path):
                    protected_io.append(f"open:{path_value(path)}")
                    raise AssertionError("protected path accessed during import/factory construction")
            elif event == "os.open" and args:
                path, flags = args[0], args[1]
                if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC):
                    write_attempts.append(f"os.open:{path_value(path)}")
                    raise AssertionError("low-level file write attempted during import/factory construction")
                if is_protected(path):
                    protected_io.append(f"os.open:{path_value(path)}")
                    raise AssertionError("protected path accessed during import/factory construction")
            elif event in {"os.mkdir", "os.makedirs", "os.rmdir", "os.remove", "os.unlink", "os.rename", "os.replace"}:
                path = args[0] if args else None
                write_attempts.append(f"{event}:{path_value(path)}")
                raise AssertionError(f"filesystem mutation attempted: {event}")

        original_thread_start = threading.Thread.start
        original_low_level_start = _thread.start_new_thread
        original_create_connection = socket.create_connection
        original_getaddrinfo = socket.getaddrinfo
        original_gethostbyname = socket.gethostbyname
        original_open = builtins.open
        threading.Thread.start = reject_thread_start
        _thread.start_new_thread = reject_low_level_thread
        socket.create_connection = lambda *a, **k: reject_network("socket.create_connection")
        socket.getaddrinfo = lambda *a, **k: reject_network("socket.getaddrinfo")
        socket.gethostbyname = lambda *a, **k: reject_network("socket.gethostbyname")
        sys.addaudithook(audit)

        try:
            import interview_copilot
            import interview_copilot.app
            import interview_copilot.lifecycle
            import interview_copilot.settings
            from fastapi import FastAPI

            settings = interview_copilot.Settings()
            app = interview_copilot.create_app(settings)
            assert isinstance(app, FastAPI)
            assert app.state.settings is settings
        finally:
            audit_active = False
            threading.Thread.start = original_thread_start
            _thread.start_new_thread = original_low_level_start
            socket.create_connection = original_create_connection
            socket.getaddrinfo = original_getaddrinfo
            socket.gethostbyname = original_gethostbyname
            builtins.open = original_open

        after_entries = snapshot()
        new_threads = [
            thread.name
            for thread in threading.enumerate()
            if thread.ident not in before_threads
        ]
        print(json.dumps({
            "before_entries": before_entries,
            "after_entries": after_entries,
            "thread_starts": thread_starts,
            "new_threads": new_threads,
            "network_calls": network_calls,
            "protected_io": protected_io,
            "write_attempts": write_attempts,
        }))
        assert before_entries == after_entries
        assert thread_starts == []
        assert network_calls == []
        assert protected_io == []
        assert write_attempts == []
        """
    )
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    result = subprocess.run(
        [sys.executable, "-I", "-c", probe, str(SRC_ROOT), str(PROJECT_ROOT)],
        cwd=cwd,
        env={**__import__("os").environ},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed["before_entries"] == observed["after_entries"]
    assert observed["thread_starts"] == []
    assert observed["new_threads"] == []
    assert observed["network_calls"] == []
    assert observed["protected_io"] == []
    assert observed["write_attempts"] == []
