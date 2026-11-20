import json
import threading

import pytest

from interview_copilot.services.corpus import CorpusIndex, passages, source_settings


@pytest.fixture
def index():
    corpus = CorpusIndex()
    yield corpus
    corpus.close()


def document(key, text, *, source=None):
    return {"id": key, "source": source or "notes:" + key + ".md", "text": text}


def test_chinese_english_aliases_and_citations(index):
    index.update_documents("test", [document("postgresql", "# 数据库\n## 查询分析\nPostgreSQL 的 EXPLAIN ANALYZE 会实际执行查询。"),
                                    document("other", "# 绘画\n透视和构图需要观察。")])
    for query in ("PG EXPLAIN", "Postgres 查询分析", "查询分析"):
        evidence = index.search(query)
        assert evidence[0]["source"] == "notes:postgresql.md"
        assert evidence[0]["heading"] == "数据库 / 查询分析"
        assert evidence[0]["lines"] == [2, 3]
    assert not index.search("galaxyphotometry")
    assert not index.search('" OR * ; DROP TABLE passages; --')


def test_chunks_preserve_lines_skip_frontmatter_and_ignore_code_headings():
    text = '---\ntags: [private_metadata]\n---\n# Real heading\n```python\n# Code comment\nprint(1)\n```\n## Next\nContent'
    chunks = list(passages(text))
    assert chunks[0].line_start == 4 and chunks[0].line_end == 8
    assert chunks[0].heading == "Real heading" and "Code comment" in chunks[0].text
    assert "private_metadata" not in "".join(chunk.text for chunk in chunks)
    assert chunks[1].heading == "Real heading / Next"
    assert max(len(chunk.text) for chunk in passages("x" * 5000, size=850)) <= 850


def test_source_updates_deletes_duplicate_names_and_withdrawn_notes(index, tmp_path):
    first, second = tmp_path / "one", tmp_path / "two"
    first.mkdir()
    second.mkdir()
    (first / "note.md").write_text("# Alpha\nuniquealpha evidence", encoding="utf-8")
    (second / "note.md").write_text("# Beta\nuniquebeta evidence", encoding="utf-8")
    (first / "withdrawn.md").write_text("> 已作废：请使用另一版\nretiredneedle", encoding="utf-8")
    (first / ".obsidian").mkdir()
    (first / ".obsidian" / "secret.md").write_text("ignoredneedle", encoding="utf-8")
    sources = [{"label": "one", "path": str(first)}, {"label": "two", "path": str(second)}]
    index.sync_sources(sources)
    assert index.stats()["files"] == 2 and index.stats()["skipped"] == 1
    assert index.search("uniquealpha")[0]["source"] == "one:note.md"
    assert index.search("uniquebeta")[0]["source"] == "two:note.md"
    assert not index.search("retiredneedle") and not index.search("ignoredneedle")
    (first / "note.md").write_text("# Alpha\nreplacementneedle", encoding="utf-8")
    (second / "note.md").unlink()
    index.sync_sources(sources)
    assert not index.search("uniquealpha") and not index.search("uniquebeta")
    assert index.search("replacementneedle")


def test_json_budget_includes_metadata_separators_and_escaping(index):
    index.update_documents("test", [document(str(number), '# Memory\n' + ('memory "quoted"\n' * 80)) for number in range(5)])
    for budget in (199, 200, 350, 900, 2400):
        evidence = index.search("memory", budget=budget)
        assert len(json.dumps(evidence, ensure_ascii=False)) <= budget
        assert all(item["excerpt"] and item["kind"] == "reference" for item in evidence)


def test_shortened_excerpt_cannot_claim_lines_that_were_not_sent(index):
    text = "# Reference\n" + "\n".join(f"evidenceneedle detail line {index}" for index in range(20))
    index.update_documents("test", [document("reference", text)])
    evidence = index.search("evidenceneedle", budget=350, limit=1)
    assert evidence[0]["excerpt_truncated"]
    assert evidence[0]["lines"][1] == evidence[0]["lines"][0] + len(evidence[0]["excerpt"].splitlines()) - 1
    assert evidence[0]["lines"][1] < 21
    assert evidence[0]["citation"] == f"〔notes:reference.md·L1-{evidence[0]['lines'][1]}〕"


def test_single_document_can_supply_more_than_two_relevant_sections(index):
    text = "\n".join(f"## Redis condition {number}\nRedis mechanism {number} has a distinct failure condition." for number in range(4))
    index.update_documents("test", [document("reference", text)])
    evidence = index.search("Redis condition", budget=4000, limit=4)
    assert len(evidence) == 4 and len({tuple(item["lines"]) for item in evidence}) == 4


def test_named_concept_does_not_import_other_sections_sharing_document_title(index):
    index.update_documents("notes", [document("cache", (
        "# Cache consistency\n"
        "## Single-flight\nSingle-flight coalesces concurrent loads without coordinating writers.\n"
        "## Versions\nCAS must compare against a retained version atomically.\n"
        "## Expiration\nTTL bounds one write, subject to latency assumptions."
    ))])
    evidence = index.search("cache consistency single-flight", budget=4000)
    assert len(evidence) == 1 and "Single-flight" in evidence[0]["excerpt"]
    compared = index.search("single-flight vs CAS", budget=4000)
    assert len(compared) == 2
    assert any("CAS" in item["excerpt"] for item in compared)
    assert len(index.search("cache consistency", budget=4000)) == 3


def test_missing_english_anchor_keeps_chinese_retrieval_fallback(index):
    index.update_documents("notes", [document("cache", "# 缓存一致性\n版本校验需要原子检查与写入，并保留最新版本。")])
    assert index.search("CAS 版本校验")[0]["source"] == "notes:cache.md"


def test_changed_document_metadata_is_reindexed_and_cancellation_keeps_old_snapshot(index, tmp_path):
    index.update_documents("files", [document("stable", "stabletoken", source="old.md")])
    cancelled = threading.Event()
    cancelled.set()
    (tmp_path / "new.md").write_text("newtoken", encoding="utf-8")
    index.sync_sources([{"label": "test", "path": str(tmp_path)}], cancelled=cancelled)
    assert index.search("stabletoken")[0]["source"] == "old.md"
    assert not index.search("newtoken") and not index.stats()["loading"]
    index.update_documents("files", [document("stable", "stabletoken", source="new.md")])
    assert index.search("stabletoken")[0]["source"] == "new.md"


def test_credentials_are_redacted_before_search(index):
    credential = "sk-" + "synthetic" * 5
    index.update_documents("test", [document("credential", "credentialneedle " + credential)])
    result = json.dumps(index.search("credentialneedle"))
    assert credential not in result and "[REDACTED]" in result


def test_source_config_explicit_empty_list_disables_local_manifest(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "corpus_sources.json").write_text(
        json.dumps({"sources": [{"label": "test", "path": "does-not-exist"}]}), encoding="utf-8")
    assert source_settings({"corpus": {"sources": []}}, tmp_path) == []
    assert source_settings({}, tmp_path)[0]["label"] == "test"


def test_source_configuration_rejects_duplicate_labels(index, tmp_path):
    with pytest.raises(ValueError):
        index.sync_sources([{"label": "same", "path": str(tmp_path)}] * 2)
    assert not index.stats()["loading"]


def test_close_is_repeatable_and_queries_after_close_are_empty(index):
    index.close()
    index.close()
    assert index.search("anything") == [] and index.stats()["closed"]


def test_navigation_only_sections_do_not_displace_real_explanations(index):
    index.update_documents("notes", [document("navigation", "# Agent LLM\n## 相关\n- [[Agent]]\n- [[LLM]]\n"),
                                     document("actual", "# Agent LLM\nAgent uses feedback from tools and LLM output to decide its next action.")])
    assert [item["source"] for item in index.search("Agent LLM")] == ["notes:actual.md"]


def test_named_model_matches_outrank_generic_chinese_topic_hits(index):
    index.update_documents("notes", [document("topic", "# 关系抽取\n关系抽取有许多通用的方法，可以用来建立图谱。"),
                                     document("model", "# 实验\nMirrorRE uses synthetic reference data for this relation extraction example.")])
    assert index.search("MirrorRE 关系抽取")[0]["source"] == "notes:model.md"


def test_cancellation_during_index_transaction_restores_previous_snapshot(index, tmp_path, monkeypatch):
    from interview_copilot.services import corpus

    index.update_documents("files", [document("stable", "stabletoken")])
    (tmp_path / "new.md").write_text("# New\nnewtoken paragraph\n## Another\nsecondtoken paragraph", encoding="utf-8")
    cancelled = threading.Event()
    original = corpus._substantive

    def interrupt(text):
        cancelled.set()
        return original(text)

    monkeypatch.setattr(corpus, "_substantive", interrupt)
    index.sync_sources([{"label": "new", "path": str(tmp_path)}], cancelled=cancelled)
    assert index.search("stabletoken") and not index.search("newtoken")
    monkeypatch.setattr(corpus, "_substantive", original)
    index.sync_sources([{"label": "new", "path": str(tmp_path)}])
    assert not index.search("stabletoken") and index.search("newtoken")


def test_fact_cards_sharing_a_quote_do_not_duplicate_evidence_or_fusion_scores(index):
    from types import SimpleNamespace

    shared = {"source":"notes:method.md", "title":"Method", "lines":[10,10],
              "text":"retrieval overview describes the approach"}
    detail = {"source":"notes:method.md", "title":"Method", "lines":[20,20],
              "text":"retrieval descriptors remove distracting entity names"}
    index.update_documents("facts", [dict(shared,id=str(number)) for number in range(4)] + [dict(detail,id="detail")])
    lexical = index.search("retrieval", budget=4000)
    assert len(lexical) == 2
    rows = []
    for item in reversed(lexical):
        rows.append({"doc_id":"facts:detail", "source":item["source"], "heading":"Method", "kind":"reference",
                     "line_start":item["lines"][0], "line_end":item["lines"][1], "excerpt":item["excerpt"]})
    index.semantic = SimpleNamespace(search=lambda question:rows)
    original = [item["lines"] for item in index.search("retrieval", budget=4000)]
    index.semantic = SimpleNamespace(search=lambda question:[rows[0]] + [rows[1]] * 12)
    assert [item["lines"] for item in index.search("retrieval", budget=4000)] == original


def test_semantic_question_focuses_on_mechanism_while_document_name_limits_sources(index):
    from types import SimpleNamespace

    index.update_documents("test", [{**document("method", "MethodA uses descriptors for retrieval."), "title":"MethodA"},
                                    document("other", "The other method describes retrieval.")])
    questions = []
    index.semantic = SimpleNamespace(search=lambda question:questions.append(question) or [])
    evidence = index.search("MethodA 为什么用描述符检索？")
    assert questions == ["为什么用描述符检索？"]
    assert [item["source"] for item in evidence] == ["notes:method.md"]
    index.search("MethodA")
    assert questions[-1] == "MethodA"


def test_damaged_name_matching_wrong_title_does_not_remove_protocol_passage(index):
    index.update_documents('test', [
        {**document('wrong', '# Gephi-SK\n图形编辑与可视化布局。'), 'title': 'Gephi-SK'},
        {**document('protocol', '# 工具调用规范\nFunction Call 使用 schema 定义参数，以 tool 角色返回调用结果。'), 'title': '工具调用规范'},
    ])
    evidence = index.search('Gephi-SK的高级封装，说明Function Call完整协议，schema怎么定义？')
    assert any(item['source'] == 'notes:protocol.md' for item in evidence)


def test_generic_term_shared_by_most_titles_is_not_a_named_source_filter(index):
    index.update_documents('test', [
        {**document('runtime', '# Agent Runtime\n循环、状态和停止条件。'), 'title': 'Agent Runtime'},
        {**document('trace', '# Agent Observability\nAgent Trace 应记录输入、工具调用、状态迁移和结果。'),
         'title': 'Agent Observability'},
        {**document('memory', '# Agent Memory\n记忆写入和冲突处理。'), 'title': 'Agent Memory'},
    ])

    evidence = index.search('Agent Trace 应该记录什么？')

    assert any(item['source'] == 'notes:trace.md' for item in evidence)


def test_repeated_named_title_records_still_scope_to_that_source(index):
    index.update_documents('test', [
        {**document('method-source', '# MethodA\nA general method overview.', source='notes:method.md'),
         'title': 'MethodA'},
        {**document('method-fact', 'Exact source quote.', source='notes:method.md'),
         'title': 'MethodA', 'search_text': 'MethodA 实体盲重写 检索键'},
        {**document('other', '# Other\n实体检索的其他方法。'), 'title': 'Other'},
    ])

    evidence = index.search('MethodA 实体盲重写检索键')

    assert all(item['source'] == 'notes:method.md' for item in evidence)
    assert evidence[0]['excerpt'] == 'Exact source quote.'
