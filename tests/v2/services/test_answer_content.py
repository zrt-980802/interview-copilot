import json

from interview_copilot.services.answer_content import context_sources, parse_answer
from interview_copilot.domain.models import SessionId
from interview_copilot.services.interview import SessionManager


def sources():
    return [{"id": "resume", "source": "context:resume", "kind": "candidate_material",
             "excerpt": "接口开发。未提供线上流量和上线记录。"},
            {"id": "note", "source": "notes:cache.md", "kind": "reference", "lines": [3, 5],
             "excerpt": "旧读可能在删除后回填。\n原子检查版本。\n需要保留比较基准。"}]


def test_protocol_preserves_unknown_lines_and_splits_only_first_separator():
    raw = ("Unusual preface\n【开口】结论\nExtra explanation\n"
           "【要点】条件 ::: body ::: retained\n【待确认】部署 ::: 未提供记录\n"
           "【方案】索引 ::: 如果采用\n【验证】延迟 ::: 待测试\n【方向】机制 ::: Explain")
    document = parse_answer(raw, "quick")
    assert [block["kind"] for block in document["blocks"]] == [
        "text", "conclusion", "point", "unknown", "hypothesis", "plan"]
    assert document["blocks"][0]["text"] == "Unusual preface"
    assert document["blocks"][1]["text"] == "结论\nExtra explanation"
    assert document["blocks"][2]["text"] == "body ::: retained"
    assert document["directions"] == [{"name": "机制", "description": "Explain"}]
    json.dumps(document)


def test_protocol_labels_inside_code_are_not_new_blocks_and_direction_only_is_visible():
    document = parse_answer('【开口】Example\n【要点】Code :::\n```text\n【材料】literal code\n\n```', "quick")
    assert len(document["blocks"]) == 2
    assert "【材料】literal code\n\n```" in document["blocks"][1]["text"]
    assert "<code" in document["blocks"][1]["html"]
    assert parse_answer("【方向】Only direction", "quick")["blocks"][0]["text"] == "【方向】Only direction"


def test_material_match_is_literal_and_never_promotes_a_note_to_personal_experience():
    document = parse_answer("【开口】范围\n【材料】context:resume ::: 接口开发。\n"
                            "【材料】notes:cache.md ::: 原子检查版本。\n"
                            "【材料】context:resume ::: 我增加了索引。", "quick", sources())
    blocks = document["blocks"]
    assert blocks[1]["support"] == {"status": "quote_matched", "source_indices": [0]}
    assert blocks[2]["support"] == {"status": "quote_matched", "source_indices": [1]}
    assert document["sources"][1]["kind"] == "reference"
    assert blocks[3]["support"]["status"] == "unverified"
    assert document["findings"] == [{"code": "material_quote_not_found", "block": 3}]
    assert "我增加了索引" in blocks[3]["text"]


def test_citations_check_exact_sources_ranges_and_context_sources():
    registry = sources() + [{"id": "adjacent", "source": "notes:cache.md", "kind": "reference",
                            "lines": [6, 7], "excerpt": "Additional reference."}]
    valid = parse_answer("【开口】事实〔context:resume〕，边界〔notes:cache.md·L3-7〕", "quick", registry)
    assert not valid["findings"] and all(cite["valid"] for cite in valid["blocks"][0]["citations"])
    registry[-1]["lines"] = [7, 8]
    invalid = parse_answer("【开口】Gap〔notes:cache.md·L3-8〕 unknown〔samefile·L3〕 malformed〔resume〕", "quick", registry)
    assert {item["code"] for item in invalid["findings"]} == {
        "citation_out_of_range", "citation_unknown", "citation_format_invalid"}
    assert parse_answer("【开口】Missing lines〔notes:cache.md〕", "quick", registry)["findings"]


def test_markdown_formats_code_without_active_html_images_or_javascript_links():
    raw = ('**深挖**\n\n```python\nprint("<script>")\n```\n'
           '<script>alert(1)</script>\n<img src=x onerror=alert(1)>\n'
           '[click](javascript:alert(1))\n![remote](https://example.invalid/pixel.png)')
    document = parse_answer(raw, "detail")
    html = document["blocks"][0]["html"]
    assert "<strong>深挖</strong>" in html and '<code class="language-python">' in html
    assert "<script>" not in html and "<img" not in html and 'href="javascript:' not in html
    assert "&lt;script&gt;" in html


def test_code_citations_are_literal_and_markdown_links_open_separately():
    document = parse_answer('Code: `〔not-a-source〕`\n\n```text\n〔literal·L1〕\n```\n'
                            '[reference](https://example.invalid)', "detail")
    assert not document["findings"] and not document["blocks"][0]["citations"]
    assert 'rel="noopener noreferrer"' in document["blocks"][0]["html"]
    assert 'target="_blank"' in document["blocks"][0]["html"]


def test_context_sources_use_only_the_supplied_excerpt_and_keep_source_types():
    reference = {"resume": "selected resume", "candidate_speech": "actual speech", "jd": "job only",
                 "context_coverage": {"partial_fields": ["resume"]}, "evidence": sources()[1:]}
    registry = context_sources(reference)
    assert registry[0]["partial"] and registry[0]["excerpt"] == "selected resume"
    assert registry[1]["kind"] == "candidate_speech" and registry[2]["kind"] == "job_description"
    registry[-1]["excerpt"] = "changed"
    assert reference["evidence"][0]["excerpt"] != "changed"


def test_answer_document_and_sources_are_isolated_across_turns_and_snapshots():
    manager, session = SessionManager(), SessionId("session")
    manager.create_session(session)
    first = manager.start_turn(session, "First")
    registry = sources()
    assert manager.set_sources(first, registry)
    registry[0]["excerpt"] = "caller mutation"
    manager.finish(first, "【开口】范围\n【材料】context:resume ::: 接口开发。")
    initial = manager.turn(session, first.turn_id).answers[0].document
    assert initial["blocks"][1]["support"]["status"] == "quote_matched"
    initial["sources"][0]["excerpt"] = "snapshot mutation"
    assert manager.turn(session, first.turn_id).answers[0].document["sources"][0]["excerpt"] != "snapshot mutation"
    detail = manager.start_task(session, first.turn_id, "detail", "Scope")
    manager.set_sources(detail, sources()[1:])
    manager.start_turn(session, "Second")
    manager.finish(detail, "Details〔notes:cache.md·L3〕")
    documents = manager.turn(session, first.turn_id).answers
    assert documents[1].document["sources"][0]["kind"] == "reference"
    manager.clear(session)
    assert not manager.set_sources(first, sources())
