import json

from fastapi.testclient import TestClient

from interview_copilot.services.knowledge import KnowledgeStore


def test_fact_review_and_source_api_feed_grounded_answers_without_bypassing_access_controls(legacy_server):
    server = legacy_server
    server._RUNTIME.knowledge = KnowledgeStore(server._RUNTIME.library,server._RUNTIME.workers,
        lambda *args:{"facts":[{"category":"project","subject":"Graph project","field":"action",
            "value":"Implemented entity linking.","quote":"Implemented entity linking.","line_start":1,"line_end":1,"claim":"stated"}]})
    client = TestClient(server.app,base_url="http://127.0.0.1")
    uploaded = client.post('/api/files/upload',data={"kind":"candidate"},files={"files":('graph.md',b'Implemented entity linking.')}).json()
    identity = uploaded['results'][0]['file']['id']; server.run_worker(); server.run_worker()
    detail = client.get('/api/files/' + identity).json()['file']
    assert detail['knowledge']['status'] == 'ready'
    fact = detail['knowledge']['facts'][0]
    endpoint = f'/api/files/{identity}/facts/{fact["id"]}'
    assert client.patch(endpoint,json={"decision":"confirm"},headers={"Origin":"https://foreign.invalid"}).status_code == 403
    assert client.patch(endpoint,json={"decision":"confirm"}).status_code == 200
    assert '用户已确认' in server._RUNTIME.knowledge.profile()
    evidence = server.answer_evidence('你如何实现 entity linking')
    assert evidence[0]['fact_check']['state'] == 'confirmed'
    assert len(json.dumps(evidence,ensure_ascii=False)) <= 2400
    assert client.get(f'/api/files/{identity}/source?start=1&end=1').json()['text'] == 'Implemented entity linking.'
    assert client.get(f'/api/files/{identity}/source?start=1&end=999').status_code == 400
    assert client.patch(endpoint,json={"decision":[]}).status_code == 400
    assert client.patch(endpoint,json={"decision":"reject"}).status_code == 200
    assert server.answer_evidence('你如何实现 entity linking')[0]['fact_check']['state'] == 'excluded'
    client.patch('/api/files/' + identity,json={"enabled":False})
    assert server.answer_evidence('entity linking') == []
