from contextlib import closing
import sqlite3
import threading
import zipfile

import pytest

from interview_copilot.services.corpus import CorpusIndex
from interview_copilot.services.documents import DocumentError, DocumentLibrary, extract_document


class DeferredWorkers:
    def __init__(self):
        self.cancelled = threading.Event()
        self.pending = []

    def start(self, target, **kwargs):
        self.pending.append(target)

    def drain(self):
        while self.pending:
            self.pending.pop(0)()


@pytest.fixture
def library(tmp_path):
    index, workers = CorpusIndex(), DeferredWorkers()
    store = DocumentLibrary(tmp_path / "library", index, workers)
    store.start()
    yield store, index, workers
    workers.cancelled.set(); store.close(); index.close()


def upload(store, tmp_path, name="notes.md", text="PostgreSQL query plan reference document, not personal experience.", kind="reference"):
    source = tmp_path / "input.txt"
    source.write_text(text, encoding="utf-8")
    return store.add(source, name, kind)["file"]


def test_failed_document_delete_removes_original_and_allows_reupload(library, tmp_path):
    store, index, workers = library
    file = upload(store, tmp_path, text=' ')
    workers.drain()
    original = store.original(file['id'])[0]
    assert store.get(file['id'])['status'] == 'error'
    store.delete(file['id'])
    assert not original.exists() and not store.list()
    assert not store._text_path(file['id']).exists()
    replacement = upload(store, tmp_path, text=' ')
    assert replacement['id'] != file['id']
    store.delete(replacement['id'])
    workers.drain()
    assert not store.list()


@pytest.mark.parametrize('state', ['ready', 'disabled', 'archived', 'queued'])
def test_delete_removes_any_document_and_retrieval_membership(library, tmp_path, state):
    store, index, workers = library
    file = upload(store, tmp_path)
    if state != 'queued':
        workers.drain()
    if state == 'disabled':
        store.update(file['id'], {'enabled': False})
    elif state == 'archived':
        store.update(file['id'], {'archived': True})
    original = store.original(file['id'])[0]
    store.delete(file['id'])
    workers.drain()
    assert not store.list() and not index.search('PostgreSQL')
    assert not original.exists() and not store._text_path(file['id']).exists()
    with pytest.raises(KeyError):
        store.get(file['id'])


def test_delete_during_extraction_cannot_recreate_text_or_index(library, tmp_path):
    store, index, workers = library
    file = upload(store, tmp_path)
    def delete_during_parse(path):
        text = extract_document(path)
        store.delete(file['id'])
        return text
    store.extractor = delete_during_parse
    workers.drain()
    assert not store.list() and not index.search('PostgreSQL')
    assert not store._text_path(file['id']).exists()


def test_delete_rollback_restores_original_text_and_catalog(library, tmp_path, monkeypatch):
    store, index, workers = library
    file = upload(store, tmp_path)
    workers.drain()
    original = store.original(file['id'])[0]
    def fail(conn):
        raise OSError('synthetic index failure')
    monkeypatch.setattr(store, '_sync_index', fail)
    with pytest.raises(OSError):
        store.delete(file['id'])
    assert original.exists() and store._text_path(file['id']).exists()
    assert store.get(file['id'])['status'] == 'ready' and index.search('PostgreSQL')


def test_deleting_current_resume_does_not_reactivate_an_old_resume(library, tmp_path):
    store, index, workers = library
    old = upload(store, tmp_path, kind='resume')
    workers.drain()
    current = upload(store, tmp_path, text='Current resume describes Java API integration.', kind='resume')
    workers.drain()
    store.delete(current['id'])
    assert store.resume() == ('', None)
    assert not store.get(old['id'])['enabled']


def test_delete_keeps_other_documents_and_their_retrieval(library, tmp_path):
    store, index, workers = library
    removed = upload(store, tmp_path)
    kept = upload(store, tmp_path, name='kept.md', text='Redis cache expiration technical reference remains available.')
    workers.drain()
    store.delete(removed['id'])
    assert [item['id'] for item in store.list()] == [kept['id']]
    assert not index.search('PostgreSQL') and index.search('Redis')
    assert store.original(kept['id'])[0].exists()


def test_resume_replacement_preserves_originals_and_identical_files_do_not_duplicate(library, tmp_path):
    store, index, workers = library
    first = upload(store,tmp_path,"resume.md","Candidate worked on Python order APIs and request validation.","resume")
    assert store.resume() is None and store.get(first["id"])["status"] == "queued"
    workers.drain()
    second = upload(store,tmp_path,"resume.md","Updated resume describes Java and integration testing.","resume")
    assert first["id"] != second["id"] and first["enabled"] and second["enabled"]
    duplicate = store.add(tmp_path / "input.txt","different-name.md","reference")
    assert duplicate["duplicate"] and duplicate["file"]["id"] == second["id"]
    assert "Python" in store.resume()[0]
    workers.drain()
    assert "Java" in store.resume()[0]
    assert not store.get(first["id"])["enabled"]
    reopened = DocumentLibrary(store.root, index, workers, extractor=lambda path: pytest.fail("ready files must reuse parsed text"))
    reopened.start()
    assert len(reopened.list()) == 2 and "Java" in reopened.resume()[0]
    assert reopened.get(first["id"],preview=True)["preview"].startswith("Candidate")
    assert store.original(first["id"])[0].read_text() == "Candidate worked on Python order APIs and request validation."
    reopened.close()


def test_file_scope_classification_and_archive_update_retrieval(library, tmp_path):
    store, index, workers = library
    file = upload(store,tmp_path)
    workers.drain()
    assert index.search("PostgreSQL")[0]["kind"] == "reference"
    store.update(file["id"],{"kind":"candidate","title":"本人项目"})
    assert index.search("PostgreSQL")[0]["kind"] == "candidate_material"
    store.update(file["id"],{"enabled":False})
    assert index.search("PostgreSQL") == []
    store.update(file["id"],{"enabled":True})
    store.update(file["id"],{"archived":True})
    assert index.search("PostgreSQL") == [] and store.original(file["id"])[0].is_file()
    store.update(file["id"],{"archived":False})
    assert not store.get(file["id"])["enabled"]
    store.update(file["id"],{"enabled":True})
    assert index.search("PostgreSQL")
    with pytest.raises(DocumentError):
        store.update(file["id"],{"kind":[]})


def test_failed_initialization_can_retry_and_pending_work_resumes_after_restart(library,tmp_path):
    store,index,workers = library
    file = upload(store,tmp_path)
    store.extractor = lambda path: (_ for _ in ()).throw(DocumentError("合成解析失败"))
    workers.drain()
    assert store.get(file["id"])["status"] == "error"
    assert store.get(file["id"])["error"] == "合成解析失败" and not index.search("PostgreSQL")
    store.extractor = extract_document
    store.retry(file["id"])
    workers.drain()
    assert store.get(file["id"])["status"] == "ready"
    with closing(sqlite3.connect(store.root / "catalog.db")) as conn, conn:
        conn.execute("UPDATE documents SET status='parsing'")
    reopened = DocumentLibrary(store.root,index,workers)
    reopened.start()
    assert reopened.get(file["id"])["status"] == "queued"
    workers.drain()
    assert reopened.get(file["id"])["status"] == "ready"
    reopened.close()


def test_archive_during_parse_does_not_reenable_or_leave_a_stuck_file(library,tmp_path):
    store,index,workers = library
    file = upload(store,tmp_path)
    def archive_while_parsing(path):
        store.update(file["id"],{"archived":True})
        return extract_document(path)
    store.extractor = archive_while_parsing
    workers.drain()
    assert store.get(file["id"])["status"] == "queued" and not index.search("PostgreSQL")
    store.extractor = extract_document
    store.update(file["id"],{"archived":False})
    workers.drain()
    assert store.get(file["id"])["status"] == "ready" and not store.get(file["id"])["enabled"]


def test_docx_paragraphs_and_tables_extract_without_unpacking_and_bad_documents_fail(tmp_path):
    path = tmp_path / "candidate.docx"
    with zipfile.ZipFile(path,"w") as archive:
        archive.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Candidate experience</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>Project result</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>')
    assert extract_document(path) == "Candidate experience\nProject result"
    binary = tmp_path / "bad.txt"; binary.write_bytes(b"not\x00text")
    with pytest.raises(DocumentError,match="UTF-8"):
        extract_document(binary)
    tiny = tmp_path / "empty.txt"; tiny.write_text(" ")
    with pytest.raises(DocumentError,match="OCR"):
        extract_document(tiny)


def test_disabled_managed_resume_does_not_silently_restore_legacy(library,tmp_path):
    store,index,workers = library
    file = upload(store,tmp_path,"resume.md",kind="resume")
    workers.drain()
    store.update(file["id"],{"enabled":False})
    assert store.resume() == ("",None)
    store.use_legacy_resume()
    assert store.resume() is None and not index.search("PostgreSQL")
