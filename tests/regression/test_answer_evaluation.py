from pathlib import Path

from scripts.evaluate_answers import inspect_output, load_suite


def test_fixed_cases_have_unique_ids_and_explicit_review_criteria():
    suite = load_suite(Path(__file__).resolve().parents[1] / "evaluation" / "answer_cases.json")
    assert len(suite["cases"]) == len({case["id"] for case in suite["cases"]}) == 50
    assert all(len(case["rubric"]) >= 2 and case["resume"] for case in suite["cases"])
    assert {"experience", "ambiguous", "principle", "rag"} <= {case["category"] for case in suite["cases"]}


def test_review_does_not_accept_unknown_citations_or_incomplete_generations():
    case = {"id": "synthetic", "mode": "quick", "category": "principle", "rubric": ["Review correctness"]}
    result = {"content": "【开口】Claim 〔synthetic:known.md·L9〕", "status": "complete",
              "retrieved_evidence": [{"source": "synthetic:known.md", "lines": [2, 4]}]}
    review = inspect_output(case, result)
    assert review["automated_findings"] == ["citation_outside_retrieved_evidence"]
    assert review["human_review_required"]
    result["content"] = "【开口】Claim 〔synthetic:known.md·L2-L4〕"
    assert not inspect_output(case, result)["automated_findings"]
    result["retrieved_evidence"].append({"source": "synthetic:known.md", "lines": [5, 8]})
    result["content"] = "【开口】Claim 〔synthetic:known.md·L2-L8〕"
    assert not inspect_output(case, result)["automated_findings"]
    result["retrieved_evidence"][1]["lines"] = [6, 8]
    assert "citation_outside_retrieved_evidence" in inspect_output(case, result)["automated_findings"]
    result["status"] = "deadline"
    assert "generation_incomplete" in inspect_output(case, result)["automated_findings"]


def test_review_flags_malformed_citations_instead_of_silently_ignoring_them():
    case = {"id": "synthetic", "mode": "detail", "category": "principle", "rubric": ["Correct source"]}
    result = {"content": "Claim 〔reference·known.md L2-L4〕", "status": "complete",
              "retrieved_evidence": [{"source": "synthetic:known.md", "lines": [2, 4]}]}
    assert inspect_output(case, result)["automated_findings"] == ["citation_format_invalid"]
    result["content"] = "Claim 〔synthetic:known.md·L2-L4"
    assert inspect_output(case, result)["automated_findings"] == ["citation_format_invalid"]
