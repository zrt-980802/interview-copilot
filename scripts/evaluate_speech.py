"""Replay synthetic timing against the actual legacy merger and the new coordinator."""

import argparse
import ast
import heapq
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from interview_copilot.domain.speech import Recognition, SpeechWindow
from interview_copilot.services import question_flow
from interview_copilot.services.speech_turns import TurnCoordinator


class VirtualClock:
    def __init__(self):
        self.now, self.jobs, self.seq = 100.0, [], 0

    def time(self):
        return self.now

    def timer(self, delay, callback):
        clock = self

        class Timer:
            cancelled = False

            def start(self):
                clock.seq += 1
                heapq.heappush(clock.jobs, (clock.now + delay, clock.seq, self))

            def cancel(self):
                self.cancelled = True

            def is_alive(self):
                return not self.cancelled

            def run(self):
                if not self.cancelled:
                    self.cancelled = True
                    callback()

        return Timer()

    def advance(self, milliseconds):
        target = 100 + milliseconds / 1000
        while self.jobs and self.jobs[0][0] <= target:
            self.now, _, job = heapq.heappop(self.jobs)
            job.run()
        self.now = target


def legacy_merger(clock, commits):
    tree = ast.parse((ROOT / "server.py").read_text(encoding="utf-8-sig"))
    definition = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "QuestionMerger")
    namespace = {"threading": SimpleNamespace(Lock=threading.Lock, Timer=clock.timer),
                 "time": clock, "_INTERVIEW_LOCK": threading.RLock(), "MERGE_WAIT_MS": 1100,
                 "CONTINUATION_MS": 8000, "CFG": {"merge": {"incomplete_wait_ms": 4000}},
                 "SESSIONS": None, "_SESSION_ID": None,
                 **{name: getattr(question_flow, name) for name in
                    ("is_non_question", "is_background", "looks_unfinished", "is_continuation")}}
    exec(compile(ast.Module(body=[definition], type_ignores=[]), str(ROOT / "server.py"), "exec"), namespace)
    return namespace["QuestionMerger"](lambda text: commits.append({"at": round((clock.time() - 100) * 1000), "text": text}))


def replay(case):
    clock, legacy, current, states = VirtualClock(), [], [], []
    merger = legacy_merger(clock, legacy)
    turns = TurnCoordinator(clock=clock.time)
    for event in case["events"]:
        clock.advance(event["at"])
        kind = event["type"]
        if kind == "speech":
            turns.activity(True, event["end"] * 16)
        elif kind == "quiet":
            turns.activity(False, turns.speech_end)
        elif kind == "window":
            turns.register(SpeechWindow(event["seq"], event["start"] * 16, event["end"] * 16))
        elif kind == "transcript":
            merger.add_segment(event["text"])
            turns.transcript(turns.draft_id, event["seq"], Recognition(event["text"]),
                             revision=event.get("revision", 0), final=event.get("final", True))
        elif kind == "eot":
            token = turns.endpoint_request()
            if token:
                turns.endpoint_result(token, event["probability"])
        else:
            raise ValueError(f"unknown replay event {kind}")
        proposal = turns.proposal()
        if proposal and turns.accept(proposal) and proposal["decision"] == "accept_question":
            current.append({"at": event["at"], "text": proposal["text"]})
        states.append({"at": event["at"], "state": turns.snapshot()["state"]})
    clock.advance(case["events"][-1]["at"] + 12000)
    expected = case["expected"]
    passed = (not current) if expected is None else (
        len(current) == 1 and current[0]["text"] == expected and current[0]["at"] >= case["not_before_ms"])
    return {"id": case["id"], "passed": passed, "legacy": legacy, "continuous": current, "states": states}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixtures", type=Path, default=ROOT / "tests/fixtures/speech/scenarios.json")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    cases = json.loads(args.fixtures.read_text(encoding="utf-8"))["cases"]
    result = {"kind": "synthetic-event-replay-not-real-ASR", "cases": [replay(case) for case in cases]}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"cases": len(cases), "passed": sum(item["passed"] for item in result["cases"]),
                      "legacy_early_submissions": sum(any(c["at"] < case.get("not_before_ms", 0) for c in item["legacy"])
                                                       for case, item in zip(cases, result["cases"]))}))
    return 0 if all(item["passed"] for item in result["cases"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
