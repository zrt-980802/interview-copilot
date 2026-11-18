"""Validate lexical retrieval for the Agent interview knowledge pack."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from interview_copilot.services.corpus import CorpusIndex  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--show-results", action="store_true")
    args = parser.parse_args()

    source = ROOT / "knowledge" / "agent-interview"
    cases = json.loads(
        (ROOT / "tests" / "evaluation" / "agent_knowledge_retrieval.json").read_text(
            encoding="utf-8"
        )
    )
    index = CorpusIndex()
    try:
        index.sync_sources([{"label": "agent", "path": str(source)}])
        failures = []
        for case in cases:
            results = index.search(case["query"], budget=5000, limit=args.limit)
            sources = [item["source"].split(":", 1)[-1] for item in results]
            passed = any(source_name == case["expected"] for source_name in sources)
            if not passed:
                failures.append({**case, "actual": sources})
            if args.show_results or not passed:
                status = "PASS" if passed else "FAIL"
                print(f"{status} {case['query']}")
                for item in results:
                    print(f"  {item['source']} | {item['heading']}")
        print(
            json.dumps(
                {"ok": not failures, "cases": len(cases), "failed": len(failures),
                 "index": index.stats()},
                ensure_ascii=False,
            )
        )
        return 0 if not failures else 1
    finally:
        index.close()


if __name__ == "__main__":
    raise SystemExit(main())
