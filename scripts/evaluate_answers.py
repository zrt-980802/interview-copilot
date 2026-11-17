"""Fixed synthetic answer cases; structural checks never replace human review."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from scripts.probe_live_model import probe
from interview_copilot.services.answer_content import parse_answer


def load_suite(path):
    with Path(path).open(encoding="utf-8") as source:
        suite = json.load(source)
    if suite.get("version") != 1 or suite.get("material_policy") != "synthetic_only":
        raise ValueError("only version-1 synthetic suites are accepted")
    seen = set()
    for case in suite["cases"]:
        if not case["id"] or case["id"] in seen or case["mode"] not in {"quick", "detail"}:
            raise ValueError("invalid or duplicate case identity")
        seen.add(case["id"])
        if not case.get("question", "").strip() or not case.get("rubric"):
            raise ValueError("cases require a question and review rubric")
        for reference in case.get("references", []):
            if not reference["source"].startswith("synthetic:") or not reference.get("text"):
                raise ValueError("references must be synthetic and source-addressable")
        case.setdefault("resume", suite["default_resume"])
    return suite


def inspect_output(case, result):
    content = result["content"]
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    findings = []
    if result["status"] != "complete":
        findings.append("generation_incomplete")
    if case["mode"] == "quick":
        if not lines or not lines[0].startswith("【开口】") or len(lines) > 7:
            findings.append("quick_protocol_invalid")
        if any(not line.startswith(("【开口】", "【要点】", "【方向】", "【材料】", "【待确认】", "【方案】", "【验证】")) for line in lines):
            findings.append("unexpected_quick_line")
    if len(content) > (400 if case["mode"] == "quick" else 600):
        findings.append("target_length_exceeded")
    document = parse_answer(content, case["mode"], result.get("source_registry", result.get("retrieved_evidence", [])))
    for finding in document["findings"]:
        code = finding["code"]
        if code in {"citation_unknown", "citation_out_of_range", "citation_lines_required"}:
            findings.append("citation_outside_retrieved_evidence")
        elif code in {"citation_format_invalid", "citation_incomplete"}:
            findings.append("citation_format_invalid")
        elif code == "material_quote_not_found":
            findings.append(code)
    return {"case_id": case["id"], "category": case["category"], "result": result,
            "automated_findings": sorted(set(findings)), "human_review_required": True,
            "review_rubric": case["rubric"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, default=ROOT / "tests" / "evaluation" / "answer_cases.json")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--case", action="append", dest="case_ids")
    parser.add_argument("--all", action="store_true", help="explicitly run every synthetic case")
    parser.add_argument("--prompt-set", type=Path, help="use a saved quick/detail prompt pair for a controlled comparison")
    parser.add_argument("--base-url")
    parser.add_argument("--model")
    parser.add_argument("--key-stdin", action="store_true")
    parser.add_argument("--max-tokens", type=int, default=1200)
    parser.add_argument("--deadline-seconds", type=float, default=45)
    parser.add_argument("--disable-thinking", action="store_true")
    parser.add_argument("--enable-thinking", action="store_true")
    args = parser.parse_args()
    if args.disable_thinking and args.enable_thinking:
        parser.error("choose one reasoning option")
    try:
        suite = load_suite(args.suite)
    except (OSError, ValueError, KeyError) as error:
        parser.error("invalid suite: " + type(error).__name__)
    if args.validate_only:
        print(json.dumps({"cases": len(suite["cases"]), "categories": sorted({case["category"] for case in suite["cases"]}),
                          "material_policy": suite["material_policy"]}))
        return
    if args.all and args.case_ids:
        parser.error("choose --all or individual --case values")
    if not args.base_url or not args.model or args.max_tokens < 1 or args.deadline_seconds <= 0:
        parser.error("provide endpoint, model and positive limits")
    selected_ids = ([case["id"] for case in suite["cases"]] if args.all else
                    args.case_ids or ["T01", "E01", "T03"])
    by_id = {case["id"]: case for case in suite["cases"]}
    if len(set(selected_ids)) != len(selected_ids) or any(key not in by_id for key in selected_ids):
        parser.error("unknown or duplicate case id")
    prompts = None
    if args.prompt_set:
        try:
            with args.prompt_set.open(encoding="utf-8") as source:
                prompts = json.load(source)
            if set(prompts) != {"quick", "detail"} or not all(isinstance(value, str) and value for value in prompts.values()):
                raise ValueError("invalid prompt set")
        except (OSError, ValueError, TypeError):
            parser.error("invalid prompt set")
    credential = (sys.stdin.readline().strip() if args.key_stdin else
                  os.environ.get("INTERVIEW_COPILOT_LIVE_API_KEY", "").strip())
    if not credential:
        parser.error("provide a process environment key or --key-stdin")
    results = []
    for case_id in selected_ids:
        case = by_id[case_id]
        probe_args = SimpleNamespace(base_url=args.base_url, model=args.model, max_tokens=args.max_tokens,
                                     deadline_seconds=args.deadline_seconds, case=case_id, case_data=case,
                                     disable_thinking=args.disable_thinking, prompts=prompts)
        probe_args.enable_thinking = args.enable_thinking
        results.append(inspect_output(case, probe(probe_args, credential)))
    print(json.dumps({"suite_version": suite["version"],
                      "suite_sha256": hashlib.sha256(args.suite.read_bytes()).hexdigest(),
                      "results": results}, ensure_ascii=True)
          .replace(credential, "[REDACTED]"))


if __name__ == "__main__":
    main()
