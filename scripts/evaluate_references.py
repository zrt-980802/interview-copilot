"""Score labeled synthetic retrieval and locally exported recording reviews."""

import argparse
import json
import math
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from scripts.audit_project import save, timing


def retrieval(suite):
    from interview_copilot.services.answer_context import scope_reference
    from interview_copilot.services.corpus import CorpusIndex

    index = CorpusIndex()
    rows = []
    try:
        index.update_documents("evaluation", suite["documents"])
        for case in suite["cases"]:
            started = time.perf_counter()
            candidates = index.search(case["question"], limit=3)
            elapsed = (time.perf_counter() - started) * 1000
            reference = scope_reference(case["question"], {"evidence": candidates})
            evidence = reference["evidence"]
            sources = {item["source"] for item in evidence}
            personal_sources = {item["source"] for item in evidence if item["kind"] == "candidate_material"}
            scope = reference["answer_scope"]["mode"]
            rows.append({
                "id": case["id"], "kind": case["kind"], "scope": scope,
                "scope_mismatch": scope != case["scope"], "evidence": evidence,
                "missing_sources": sorted(set(case["expected"]) - sources),
                "irrelevant_candidates": [
                    item["citation"] for item in evidence if item["source"] not in case["relevant"]],
                "unexpected_personal_sources": sorted(personal_sources - set(case.get("personal_sources", []))),
                "retrieval_ms": round(elapsed, 3),
            })
    finally:
        index.close()
    return {
        "kind": "synthetic_lexical_candidates_and_scope",
        "summary": {
            "cases": len(rows),
            "cases_with_missing_sources": sum(bool(row["missing_sources"]) for row in rows),
            "cases_with_irrelevant_candidates": sum(bool(row["irrelevant_candidates"]) for row in rows),
            "irrelevant_candidates": sum(len(row["irrelevant_candidates"]) for row in rows),
            "candidates": sum(len(row["evidence"]) for row in rows),
            "scope_mismatches": sum(row["scope_mismatch"] for row in rows),
            "cases_with_unexpected_personal_sources": sum(bool(row["unexpected_personal_sources"]) for row in rows),
            "retrieval_ms": timing([row["retrieval_ms"] for row in rows]),
        },
        "cases": rows,
    }


def stage_summary(samples):
    stages = {}
    for sample in samples:
        for name, elapsed in sample.items():
            if type(elapsed) in (int, float) and math.isfinite(elapsed) and elapsed >= 0:
                stages.setdefault(name, []).append(elapsed)
    return {key: timing(values) for key, values in sorted(stages.items())}


def recording(export):
    from scripts.validate_stt import character_error_rate, normalized

    segments = (export.get("transcript") or {}).get("segments", [])
    reviewed = []
    for segment in segments:
        reference = segment.get("reviewed_text")
        reviewed_at = segment.get("reviewed_at")
        hypothesis = segment.get("text")
        if not (isinstance(reference, str) and isinstance(reviewed_at, str)
                and reviewed_at.strip() and isinstance(hypothesis, str)):
            continue
        characters = len(normalized(reference))
        edits = round(character_error_rate(reference, hypothesis) * max(1, characters))
        reviewed.append({"file": segment["file"], "source": segment.get("source"),
                         "reference_characters": characters, "edits": edits,
                         "cer": round(edits / characters, 4) if characters else None})
    characters = sum(row["reference_characters"] for row in reviewed)
    edits = sum(row["edits"] for row in reviewed)
    events = export.get("realtime", [])
    by_event = {}
    for event in events:
        if event.get("timings_ms"):
            by_event.setdefault(event["type"], []).append(event["timings_ms"])
    return {
        "kind": "local_recording_review",
        "offline_model": (export.get("transcript") or {}).get("model"),
        "segments": len(segments),
        "reviewed_segments": len(reviewed),
        "reference_characters": characters,
        "offline_cer": round(edits / characters, 4) if characters else None,
        "realtime_events": len(events),
        "realtime_cer": None,
        "realtime_cer_reason": "realtime events are not aligned to reviewed recording segments",
        "timings_by_event_ms": {name: stage_summary(samples) for name, samples in sorted(by_event.items())},
        "reviewed": reviewed,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--retrieval-suite", type=Path)
    inputs.add_argument("--recording-export", type=Path)
    args = parser.parse_args()
    path = args.retrieval_suite or args.recording_export
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    result = retrieval(value) if args.retrieval_suite else recording(value)
    save(args.output, result)
    summary = result["summary"] if args.retrieval_suite else {
        key: result[key] for key in ("segments", "reviewed_segments", "offline_cer", "realtime_cer")
    }
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
