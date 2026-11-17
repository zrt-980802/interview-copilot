import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace


def test_materials_keep_same_extension_and_same_original_names(monkeypatch, tmp_path):
    root = Path(__file__).resolve().parents[2]
    monkeypatch.setitem(sys.modules, "copilot.resume", SimpleNamespace(clean_text=lambda text: text, extract_text=lambda path: Path(path).read_text()))
    monkeypatch.setitem(sys.modules, "copilot.knowledge", SimpleNamespace(_llm_digest=lambda *a, **k: "", _llm_conf=lambda cfg: cfg))
    spec = importlib.util.spec_from_file_location("isolated_materials", root / "copilot/materials.py")
    materials = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(materials)
    materials.MAT_DIR = str(tmp_path / "archive")
    materials.META_JSON = str(tmp_path / "meta.json")
    source = tmp_path / "notes.md"
    source.write_text("first document: " + "alpha " * 30)
    a = materials.add_file(str(source), {})
    source.write_text("second document: " + "beta " * 30)
    b = materials.add_file(str(source), {})
    assert a["origin"] != b["origin"]
    assert a["source"] == b["source"] == "notes.md"
    assert len(materials.MATS) == 2
    assert "alpha" in Path(a["origin"]).read_text()
    assert "beta" in Path(b["origin"]).read_text()
    known = json.loads(Path(materials.META_JSON).read_text())["known"]
    assert set(known) == {a["origin"], b["origin"]}
    materials.MATS.clear()
    materials.reload_sync({})
    assert len(materials.MATS) == 2
    assert all(item["source"] == "notes.md" for item in materials.MATS)
    assert len({item["source"] for item in materials.corpus_documents()}) == 2


def test_paper_archives_keep_original_names_without_overwriting(monkeypatch, tmp_path):
    root = Path(__file__).resolve().parents[2]
    monkeypatch.setitem(sys.modules, "copilot.resume", SimpleNamespace(clean_text=lambda text: text, extract_text=lambda path: Path(path).read_text()))
    spec = importlib.util.spec_from_file_location("isolated_papers", root / "copilot/knowledge.py")
    knowledge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(knowledge)
    knowledge.PAPERS_DIR = str(tmp_path / "papers")
    knowledge.META_JSON = str(tmp_path / "papers_meta.json")
    knowledge._llm_digest = lambda *a, **k: ""
    source = tmp_path / "paper.pdf"
    source.write_text("synthetic paper A " * 40)
    first = knowledge.add_paper_file(str(source), {})
    source.write_text("synthetic paper B " * 40)
    second = knowledge.add_paper_file(str(source), {})
    assert first["origin"] != second["origin"]
    assert first["source"] == second["source"] == "paper.pdf"
    assert len(knowledge.PAPERS) == 2
    assert "paper A" in Path(first["origin"]).read_text()
    assert "paper B" in Path(second["origin"]).read_text()
    knowledge.PAPERS.clear()
    knowledge._reload_sync({})
    assert len(knowledge.PAPERS) == 2
    assert all(item["source"] == "paper.pdf" for item in knowledge.PAPERS)
    assert len({item["source"] for item in knowledge.corpus_documents()}) == 2
