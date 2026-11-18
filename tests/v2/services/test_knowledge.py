import json
import threading

import pytest

from interview_copilot.services.corpus import CorpusIndex
from interview_copilot.services.documents import DocumentLibrary, extract_document
from interview_copilot.services.knowledge import KnowledgeStore, checked_fact, analysis_windows
from interview_copilot.services.semantic import SemanticIndex
from interview_copilot.services.answer_context import compose_context, ContextBudgetExceeded, answer_scope


class Workers:
    def __init__(self):
        self.cancelled, self.pending = threading.Event(),[]

    def start(self, target, **kwargs):
        self.pending.append(target)

    def drain(self):
        while self.pending:
            self.pending.pop(0)()


@pytest.fixture
def library(tmp_path):
    index, workers = CorpusIndex(),Workers()
    library = DocumentLibrary(tmp_path / "documents",index,workers)
    library.start()
    yield library,index,workers
    workers.cancelled.set()
    library.close()
    index.close()


def add(library,workers,tmp_path,name,text,kind="candidate"):
    path = tmp_path / name
    path.write_text(text,encoding="utf-8")
    document = library.add(path,name,kind)["file"]
    workers.drain()
    return document


def raw(value="Implemented entity linking.",**changes):
    return {"category":"project","subject":"Graph project","field":"action","value":value,
            "quote":value,"line_start":1,"line_end":1,"claim":"stated",**changes}


def test_only_literal_quotes_and_supported_numbers_enter_facts():
    document = {"id":"test","kind":"candidate","name":"test.md"}
    text = "# Graph\nImplemented entity linking.\nMeasured F1 80.41 on the benchmark."
    fact = checked_fact(raw(),document,text)
    assert fact["lines"] == [2,2] and fact["base_state"] == "source_supported"
    assert checked_fact(raw("Improved throughput by 99%.",quote="Implemented entity linking."),document,text) is None
    assert checked_fact(raw(quote="Invented evidence sentence."),document,text) is None
    assert checked_fact(raw(category=[]),document,text) is None
    assert checked_fact(raw(),document,text,allowed_lines=(3,3)) is None
    assert checked_fact(raw(quote="implemented entity linking."),document,text) is None


@pytest.mark.parametrize("kind,claim,text,expected",[
    ("reference","stated","Implemented entity linking.","reference"),
    ("paper","stated","Implemented entity linking.","reference"),
    ("candidate","planned","Implemented entity linking.","planned"),
    ("candidate","stated","# 面试答案全集\nImplemented entity linking.","needs_confirmation"),
    ("candidate","unknown","Implemented entity linking.","needs_confirmation"),
])
def test_reference_and_draft_statements_do_not_become_personal_facts(kind,claim,text,expected):
    document = {"id":"test","kind":kind,"name":"test.md"}
    assert checked_fact(raw(claim=claim),document,text)["base_state"] == expected


def test_project_cards_persist_review_and_drop_disabled_sources(library,tmp_path):
    store,index,workers = library
    doc = add(store,workers,tmp_path,"graph.md","Implemented entity linking.")
    calls = []
    def generate(system,data):
        calls.append(data)
        return {"summary":"A graph project.","facts":[raw(),raw(quote="Missing source statement.")]}
    knowledge = KnowledgeStore(store,workers,generate)
    knowledge.schedule(); workers.drain()
    detail = knowledge.detail(doc["id"])
    assert detail["status"] == "ready" and detail["rejected_quotes"] == 1
    assert detail["projects"][0]["name"] == "Graph project"
    fact_id = detail["facts"][0]["id"]
    knowledge.review(doc["id"],fact_id,"confirm")
    assert knowledge.detail(doc["id"])["facts"][0]["state"] == "confirmed"
    knowledge.retry(doc["id"]); workers.drain()
    assert knowledge.detail(doc["id"])["facts"][0]["state"] == "confirmed"
    knowledge.review(doc["id"],fact_id,"reject")
    evidence = knowledge.evidence("你如何实现 entity linking")
    assert evidence[0]["fact_check"]["state"] == "excluded"
    assert "用户已排除" in knowledge.profile()
    annotation = knowledge.annotate(index.search("entity linking"))
    assert annotation[0]["fact_checks"][0]["state"] == "excluded"
    reopened = KnowledgeStore(store,workers,lambda *args: pytest.fail("cached analysis should be reused"))
    reopened.schedule(); workers.drain()
    assert reopened.detail(doc["id"])["facts"][0]["state"] == "excluded"
    store.update(doc["id"],{"enabled":False}); reopened.schedule(); workers.drain()
    assert reopened.evidence("entity linking") == [] and reopened.profile() == ""
    assert len(calls) == 2


def test_conflicting_graduation_dates_are_not_silently_resolved(library,tmp_path):
    store,index,workers = library
    document = add(store,workers,tmp_path,"resume.md",
                   "本人预计 2026 年 6 月毕业。\nMPhil 教育 2024.09 - 2026.10 (预计)","resume")
    knowledge = KnowledgeStore(store,workers,lambda *args:{"facts":[]})
    knowledge.schedule(); workers.drain()
    facts = knowledge.detail(document["id"])["facts"]
    assert len(facts) == 2 and all(fact["state"] == "conflict" for fact in facts)
    assert all(fact["alternatives"] for fact in facts)
    assert "材料冲突" in knowledge.profile()
    knowledge.review(document["id"],facts[1]["id"],"confirm")
    reviewed = knowledge.detail(document["id"])["facts"]
    assert [fact["state"] for fact in reviewed] == ["conflict","confirmed"]
    knowledge.review(document["id"],facts[1]["id"],"auto")
    assert all(fact["state"] == "conflict" for fact in knowledge.detail(document["id"])["facts"])


def test_distinct_education_institutions_are_not_conflicting_periods(library,tmp_path):
    store,index,workers = library
    text = "Alpha University MPhil 2024.09 - 2026.10\nBeta University MSc 2023.09 - 2024.06"
    doc = add(store,workers,tmp_path,"education.md",text,"resume")
    def generate(*args):
        return {"facts":[raw(category="education",subject="本人",field="period",value=value,quote=quote,
                            line_start=line,line_end=line) for line,value,quote in
                         [(1,"2024.09 - 2026.10",text.splitlines()[0]),(2,"2023.09 - 2024.06",text.splitlines()[1])]]}
    knowledge = KnowledgeStore(store,workers,generate); knowledge.schedule(); workers.drain()
    assert all(fact["state"] == "source_supported" for fact in knowledge.detail(doc["id"])["facts"])


def test_analysis_failure_preserves_ready_text_and_retry_recovers(library,tmp_path):
    store,index,workers = library
    doc = add(store,workers,tmp_path,"notes.md","Implemented entity linking.")
    knowledge = KnowledgeStore(store,workers,lambda *args:"not-json")
    knowledge.schedule(); workers.drain()
    assert knowledge.detail(doc["id"])["status"] == "error"
    assert store.get(doc["id"])["status"] == "ready" and index.search("entity")
    knowledge.generate = lambda *args:{"facts":[raw()]}
    knowledge.retry(doc["id"]); workers.drain()
    assert knowledge.detail(doc["id"])["status"] == "ready"


def test_html_filters_executable_markup_and_source_ranges_are_bounded(library,tmp_path):
    store,index,workers = library
    path = tmp_path / "guide.html"
    path.write_text('<style>secret_style</style><h1>架构指南</h1><p>编排器管理工具调用。</p><script>secret_script()</script><h2>检索</h2><p>语义匹配正文。</p>',encoding="utf-8")
    text = extract_document(path)
    assert text == "# 架构指南\n编排器管理工具调用。\n## 检索\n语义匹配正文。"
    doc = store.add(path,path.name)["file"]; workers.drain()
    assert "secret" not in store.get(doc["id"],preview=True)["preview"]
    assert store.source(doc["id"],3,4)["text"] == "## 检索\n语义匹配正文。"
    with pytest.raises(ValueError):
        store.source(doc["id"],1,999)


def test_analysis_windows_cover_many_short_sections_instead_of_only_first_headings():
    text = "\n".join(f"## Section {number}\nGrounded statement for section {number}." for number in range(100))
    windows = list(analysis_windows(text,size=1000))
    assert "\n".join(window.text for window in windows) == text
    assert windows[-1].line_end == len(text.splitlines())


def test_semantic_paraphrase_retrieval_caches_vectors_and_withdraws_sources(tmp_path):
    calls = []
    def encoder(texts):
        calls.append(list(texts))
        return [[1,0] if "entity" in text or "不同名称" in text else [0,1] for text in texts]
    semantic = SemanticIndex(tmp_path / "vectors.db",encoder=encoder,model="synthetic",threshold=0.8)
    doc = {"id":"graph","source":"uploads:graph/graph.md","title":"Graph","kind":"candidate_material",
           "text":"The project implemented entity resolution across databases."}
    semantic.activate(["uploads:graph"])
    semantic.sync([doc],cancelled=threading.Event())
    index = CorpusIndex(); index.update_documents("uploads",[doc]); index.semantic = semantic
    assert index.search("同一对象在不同系统中的不同名称")[0]["source"] == doc["source"]
    before = len(calls)
    assert semantic.search("同一对象在不同系统中的不同名称")
    assert len(calls) == before
    reopened = SemanticIndex(tmp_path / "vectors.db",encoder=encoder,model="synthetic",threshold=0.8)
    reopened.activate(["uploads:graph"]); reopened.sync([doc],cancelled=threading.Event())
    assert len(calls) == before and reopened.stats()["status"] == "ready"
    semantic.activate([])
    assert index.search("同一对象在不同系统中的不同名称") == []
    index.close()


def test_embedding_failure_keeps_lexical_retrieval_available(tmp_path):
    semantic = SemanticIndex(tmp_path / "vectors.db",encoder=lambda texts: [[float("nan")]],model="bad")
    doc = {"id":"graph","source":"uploads:graph/graph.md","title":"Graph","kind":"reference","text":"PostgreSQL query execution reference."}
    semantic.activate(["uploads:graph"]); semantic.sync([doc],cancelled=threading.Event())
    assert semantic.stats()["status"] == "error"
    index = CorpusIndex(); index.update_documents("uploads",[doc]); index.semantic = semantic
    assert index.search("PostgreSQL")[0]["source"] == doc["source"]
    index.close()


def test_review_constraints_survive_context_clipping_and_fail_closed_if_too_large():
    profile = "用户已排除：不能采用旧资料中的百万并发指标。" * 8
    reference = {"resume":"旧项目描述。" * 1000,"candidate_profile":profile,"evidence":[]}
    pack = compose_context("Grounded interview assistant",reference,[{"role":"user","content":"介绍项目"}],max_chars=1800)
    assert json.loads(pack.messages[1]["content"])["candidate_profile"] == profile
    assert pack.sent_chars <= 1800
    with pytest.raises(ContextBudgetExceeded,match="核对记录"):
        compose_context("system",{"candidate_profile":profile * 20},[{"role":"user","content":"介绍项目"}],max_chars=800)


def test_project_method_scope_separates_recorded_experience_from_generic_design():
    evidence = [{"kind":"candidate_material","fact_check":{"subject":"肝病时序图谱"}}]
    scope = answer_scope("肝病图谱如何消除同名歧义？",evidence,"[材料冲突] 本人 / graduation_date")
    assert scope["mode"] == "project_method" and "temporal_boundary" in scope
    assert answer_scope("GuideRE 的 EBR 有什么作用？",[],"")["mode"] == "technical"


def test_bilingual_fact_embedding_returns_original_quote_and_location(tmp_path):
    def encoder(texts):
        return [[1,0] if "中文关系模式" in text else [0,1] for text in texts]
    semantic = SemanticIndex(tmp_path / "vectors.db",encoder=encoder,model="bilingual",threshold=0.8)
    doc = {"id":"paper","source":"uploads:paper/research.pdf","title":"Method paper","kind":"paper",
           "text":"Unrelated passage describing experiment settings.",
           "units":[{"heading":"Method","line_start":30,"line_end":31,
                     "excerpt":"Rewritten descriptors serve as retrieval keys.",
                     "search_text":"中文关系模式，用重写结果做检索键。"}]}
    semantic.activate(["uploads:paper"]); semantic.sync([doc],cancelled=threading.Event())
    result = semantic.search("中文关系模式是什么？")
    assert len(result) == 1 and result[0]["line_start"] == 30
    assert result[0]["excerpt"] == doc["units"][0]["excerpt"] and "search_text" not in result[0]


def test_bilingual_fts_fact_uses_original_source_line_and_named_document_scope(library,tmp_path):
    store,index,workers = library
    quote = "Rewritten descriptors serve as retrieval keys."
    doc = add(store,workers,tmp_path,"MethodA.md","# Paper\nMethodA summarizes the approach.\n" + quote,"paper")
    add(store,workers,tmp_path,"Unrelated.md","The other system uses 检索示例 and aliases.","reference")
    fact = raw(value="实体盲重写用描述符作为示例检索键",subject="MethodA",quote=quote,line_start=3,line_end=3)
    knowledge = KnowledgeStore(store,workers,lambda *args:{"facts":[fact]})
    knowledge.schedule(); workers.drain()
    evidence = index.search("MethodA 实体盲重写检索键")
    assert all(item["source"].endswith("/MethodA.md") for item in evidence)
    assert evidence[0]["excerpt"] == quote and evidence[0]["lines"] == [3,3]


def test_pdf_page_markers_do_not_make_blank_scans_appear_searchable(tmp_path):
    from pypdf import PdfWriter
    writer = PdfWriter()
    for _ in range(4):
        writer.add_blank_page(width=400,height=600)
    path = tmp_path / "scan.pdf"
    with path.open("wb") as handle:
        writer.write(handle)
    with pytest.raises(ValueError,match="OCR"):
        extract_document(path)


def test_truncated_json_retries_only_the_failed_window_with_smaller_output(library,tmp_path):
    store,index,workers = library
    document = add(store,workers,tmp_path,"graph.md","Implemented entity linking.")
    prompts = []
    def generate(system,data):
        prompts.append(system)
        return '{"facts":[' if len(prompts) == 1 else {"facts":[raw()]}
    knowledge = KnowledgeStore(store,workers,generate); knowledge.schedule(); workers.drain()
    assert knowledge.detail(document["id"])["status"] == "ready"
    assert len(prompts) == 2 and "最多提取 8 条" in prompts[1]
    assert checked_fact(raw(claim=[]),document,"Implemented entity linking.")["base_state"] == "needs_confirmation"
    pdf = {**document,"extension":".pdf"}
    assert checked_fact(raw(value="Mechanism",quote="A truncated mechanism pro-"),pdf,"A truncated mechanism pro-") is None


def test_weak_semantic_similarity_does_not_force_unrelated_reference(tmp_path):
    def encoder(texts):
        return [[0.45,0.893] if text == "unrelated question" else [1,0] for text in texts]
    semantic = SemanticIndex(tmp_path / "vectors.db",encoder=encoder,model="synthetic")
    document = {"id":"graph","source":"uploads:graph/graph.md","title":"Graph","kind":"reference",
                "text":"Knowledge graph entity resolution reference."}
    semantic.activate(["uploads:graph"]); semantic.sync([document],cancelled=threading.Event())
    assert semantic.search("unrelated question") == []
    assert semantic.search("Knowledge graph")
