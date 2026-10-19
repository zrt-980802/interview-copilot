from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import textwrap


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"


def test_import_is_side_effect_free(tmp_path: Path) -> None:
    probe = textwrap.dedent(
        """
        import json
        from pathlib import Path
        import sys
        import threading

        sys.path.insert(0, sys.argv[1])
        workdir = Path.cwd()
        before_files = sorted(
            path.relative_to(workdir).as_posix()
            for path in workdir.rglob("*")
            if path.is_file()
        )
        before_threads = {thread.ident for thread in threading.enumerate()}
        thread_starts = []
        network_calls = []
        sensitive_io = []
        importing = True
        sensitive_roots = (
            workdir,
            Path.home() / "Desktop",
            Path.home() / "Documents",
            Path.home() / "Downloads",
            Path(sys.argv[1]).parent / "data",
        )

        def is_sensitive(path_value):
            if not isinstance(path_value, str):
                return False
            path = Path(path_value).resolve()
            return any(path == root or root in path.parents for root in sensitive_roots)

        def audit(event, args):
            if event in {"socket.connect", "socket.getaddrinfo", "urllib.Request"}:
                network_calls.append(event)
                raise AssertionError("network access attempted during import")
            if importing and event in {"open", "os.listdir", "os.scandir", "glob.glob"}:
                if args and is_sensitive(args[0]):
                    sensitive_io.append(f"{event}: {args[0]}")
                    raise AssertionError("private or runtime path accessed during import")

        def reject_thread_start(thread, *args, **kwargs):
            thread_starts.append(thread.name)
            raise AssertionError("thread started during import")

        original_thread_start = threading.Thread.start
        threading.Thread.start = reject_thread_start
        sys.addaudithook(audit)

        try:
            import interview_copilot
            import interview_copilot.app
            import interview_copilot.lifecycle
            import interview_copilot.settings
        finally:
            importing = False
            threading.Thread.start = original_thread_start

        assert callable(interview_copilot.create_app)

        after_files = sorted(
            path.relative_to(workdir).as_posix()
            for path in workdir.rglob("*")
            if path.is_file()
        )
        new_threads = [
            thread.name
            for thread in threading.enumerate()
            if thread.ident not in before_threads
        ]
        print(json.dumps({
            "before_files": before_files,
            "after_files": after_files,
            "thread_starts": thread_starts,
            "new_threads": new_threads,
            "network_calls": network_calls,
            "sensitive_io": sensitive_io,
        }))
        """
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", probe, str(SRC_ROOT)],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed["before_files"] == []
    assert observed["after_files"] == []
    assert observed["thread_starts"] == []
    assert observed["new_threads"] == []
    assert observed["network_calls"] == []
    assert observed["sensitive_io"] == []
    assert not (tmp_path / "data").exists()
    assert not any(tmp_path.glob("*.db*"))
