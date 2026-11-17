import threading

import pytest

from interview_copilot.services.workers import WorkerGroup


def test_worker_admission_errors_and_shutdown_deadline():
    workers = WorkerGroup()
    entered, release = threading.Event(), threading.Event()

    def held():
        entered.set()
        release.wait(3)

    workers.start(held)
    assert entered.wait(1)
    workers.stop()
    try:
        with pytest.raises(RuntimeError, match="stopping"):
            workers.start(lambda: None)
        with pytest.raises(TimeoutError):
            workers.join(timeout=0.01)
    finally:
        release.set()
        workers.join(timeout=2)
    assert workers.stats()["active"] == 0


def test_worker_errors_are_counted_and_threads_are_removed():
    workers = WorkerGroup()

    def broken():
        raise ValueError("synthetic")

    workers.start(broken)
    workers.join(timeout=2)
    assert workers.stats() == {"active": 0, "errors": 1, "stopping": False}
