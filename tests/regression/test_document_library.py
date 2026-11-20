from pathlib import Path

from fastapi.testclient import TestClient
import httpx
import pytest

from interview_copilot.services.remote_documents import download_github, github_markdown_url


def test_batch_initialization_is_persistent_and_reference_is_used_without_becoming_resume(legacy_server):
    server = legacy_server
    with TestClient(server.app,base_url='http://127.0.0.1') as client:
        response = client.post('/api/files/upload',data={'kind':'reference'},files=[
            ('files',('query.md',b'# PostgreSQL\nEXPLAIN ANALYZE executes a query and reports actual row counts.')),
            ('files',('notes.txt',b'Redis reference document about cache expiration and consistency.')),
            ('files',('unsupported.exe',b'do not execute'))])
        assert response.status_code == 202 and response.headers['cache-control'] == 'no-store'
        results = response.json()['results']
        assert [item['ok'] for item in results] == [True,True,False]
        assert all(item['file']['status'] == 'queued' for item in results if item['ok'])
        server.run_worker()
        first, second = [item['file']['id'] for item in results if item['ok']]
        listing = client.get('/api/files').json()['files']
        assert len(listing) == 2 and all(item['status'] == 'ready' for item in listing)
        detail = client.get('/api/files/' + first).json()['file']
        assert 'EXPLAIN' in detail['preview'] and detail['chunks'] > 0 and detail['overview']
        download = client.get('/api/files/' + first + '/download')
        assert download.content.startswith(b'# PostgreSQL') and 'attachment' in download.headers['content-disposition']
        task = server.spawn_answer('Explain PostgreSQL EXPLAIN ANALYZE')
        server.run_worker()
        call = server.LLM.calls[-1]
        assert call.resume == 'synthetic resume' and call.evidence[0]['kind'] == 'reference'
        assert '/query.md' in call.evidence[0]['source']
        assert 'origin' not in detail and 'sha256' not in detail
        call.done('【开口】合成回答')
        assert client.patch('/api/files/' + first,json={'enabled':False}).status_code == 200
        assert not any('/query.md' in item['source'] for item in server.answer_evidence('PostgreSQL EXPLAIN ANALYZE'))
        assert client.patch('/api/files/' + second,json={'archived':True}).json()['file']['archived']
        assert client.get('/api/files/not-a-real-id').status_code == 404
        assert client.patch('/api/files/' + first,json={'path':'C:/private'}).status_code == 400
    server.stop_runtime()
    server.start_runtime(**server.app.state.runtime_options)
    try:
        assert len(server._RUNTIME.library.list()) == 2
        assert not server._RUNTIME.library.get(first)['enabled']
        assert server._RUNTIME.library.get(second)['archived']
    finally:
        server.stop_runtime()


def test_initialization_failure_retry_and_single_resume_validation(legacy_server):
    server = legacy_server
    client = TestClient(server.app,base_url='http://127.0.0.1')
    invalid = client.post('/api/files/upload',data={'kind':'reference'},files={'files':('scan.txt',b' ')})
    identity = invalid.json()['results'][0]['file']['id']; server.run_worker()
    assert client.get('/api/files/' + identity).json()['file']['status'] == 'error'
    assert client.post('/api/files/' + identity + '/retry').status_code == 200
    server.run_worker()
    assert client.get('/api/files/' + identity).json()['file']['status'] == 'error'
    many = client.post('/api/files/upload',data={'kind':'resume'},files=[('files',('a.md',b'a')),('files',('b.md',b'b'))])
    assert many.status_code == 400
    resume = client.post('/api/files/upload',data={'kind':'resume'},files={'files':('resume.md',b'Candidate implemented Python API validation and integration tests.')})
    server.run_worker()
    assert 'Candidate implemented' in server.resume_text()
    assert client.get('/api/state').json()['resume']['source'] == 'resume.md'
    assert 'Candidate implemented' in server.ENGINE.hotwords['resume']
    assert client.post('/api/files/resume-source/legacy').status_code == 200
    assert server.resume_text() == 'synthetic resume'


@pytest.mark.parametrize('state', ['error', 'ready', 'queued', 'disabled', 'archived'])
def test_file_deletion_is_local_and_removes_all_states(legacy_server, state):
    s = legacy_server
    client = TestClient(s.app, base_url='http://127.0.0.1')
    identity = client.post('/api/files/upload', data={'kind':'reference'},
                           files={'files':('delete.txt', b' ' if state == 'error' else b'Unique reference describing PostgreSQL query optimization.')}).json()['results'][0]['file']['id']
    if state != 'queued':
        s.run_worker()
    if state == 'disabled':
        client.patch('/api/files/' + identity, json={'enabled': False})
    elif state == 'archived':
        client.patch('/api/files/' + identity, json={'archived': True})
    assert client.delete('/api/files/' + identity, headers={'origin':'https://external.invalid'}).status_code == 403
    assert client.delete('/api/files/' + identity).status_code == 200
    assert client.get('/api/files/' + identity).status_code == 404
    assert client.get('/api/files/' + identity + '/download').status_code == 404
    if state == 'queued':
        s.run_worker()
    assert not s._RUNTIME.library.list() and not s._RUNTIME.corpus.search('PostgreSQL')


def test_github_urls_are_limited_to_specific_public_markdown_files():
    canonical,name = github_markdown_url('https://github.com/example/project/blob/main/docs/readme.md?plain=1#section')
    assert canonical == 'https://raw.githubusercontent.com/example/project/main/docs/readme.md' and name == 'readme.md'
    for url in ('http://github.com/x/y/blob/main/a.md','https://github.com/x/y/tree/main',
                'https://github.com.evil.invalid/x/y/blob/main/a.md','https://user:secret@github.com/x/y/blob/main/a.md',
                'https://github.com/x/y/blob/main/run.exe','https://127.0.0.1/private.md','https://raw.githubusercontent.com:8443/x/y/main/a.md'):
        with pytest.raises(ValueError):
            github_markdown_url(url)


def test_github_import_preserves_reference_url_and_refuses_redirects(legacy_server,tmp_path,monkeypatch):
    server = legacy_server
    text = '# RAG\nRetrieval grounding uses relevant reference passages, not personal experience.'
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(200,content=text.encode())
    async def offline_download(url,directory):
        return await download_github(url,directory,transport=httpx.MockTransport(respond))
    monkeypatch.setattr(server,'download_github',offline_download)
    client = TestClient(server.app,base_url='http://127.0.0.1')
    response = client.post('/api/files/import-github',json={'url':'https://github.com/example/repo/blob/main/guide.md'})
    assert response.status_code == 202
    identity = response.json()['file']['id']; server.run_worker()
    detail = client.get('/api/files/' + identity).json()['file']
    assert detail['kind'] == 'reference' and detail['source_url'].startswith('https://raw.githubusercontent.com/')
    evidence = server.answer_evidence('RAG Retrieval grounding')
    assert evidence[0]['source'] == detail['source_url'] and evidence[0]['kind'] == 'reference'
    assert calls[0].headers.get('authorization') is None and not calls[0].url.query
    async def reject_redirect():
        with pytest.raises(ValueError,match='GitHub'):
            await download_github('https://github.com/example/repo/blob/main/guide.md',tmp_path,
                                  transport=httpx.MockTransport(lambda request:httpx.Response(302,headers={'location':'http://127.0.0.1/'})))
        with pytest.raises(ValueError,match='5 MB'):
            await download_github('https://github.com/example/repo/blob/main/large.md',tmp_path,
                                  transport=httpx.MockTransport(lambda request:httpx.Response(200,content=b'a' * (5 * 1024 * 1024 + 1))))
    import asyncio
    asyncio.run(reject_redirect())


def test_file_management_rejects_cross_site_requests_and_unknown_hosts(legacy_server):
    client = TestClient(legacy_server.app,base_url='http://127.0.0.1')
    for path in ('/files','/api/files'):
        assert client.get(path,headers={'Host':'untrusted.invalid'}).status_code == 403
        assert client.get(path,headers={'Origin':'https://untrusted.invalid'}).status_code == 403
    response = client.post('/api/files/upload',headers={'Sec-Fetch-Site':'cross-site'},
                           files={'files':('reference.md',b'Public reference document for query planning.')})
    assert response.status_code == 403 and response.headers['cache-control'] == 'no-store'
    assert legacy_server._RUNTIME.library.list() == []
