from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

import httpx
import numpy as np
import pytest

from copilot.stt import STTClient
from interview_copilot.domain.models import AudioSegment, SessionId
from interview_copilot.providers.http_policy import trust_env_for_url
from interview_copilot.providers.stt import OpenAICompatibleTranscriber, STTServerError


@pytest.mark.parametrize('url',[
    'http://127.0.0.1:8760/v1','http://127.0.0.2/v1','http://localhost:8760/v1',
    'http://LOCALHOST./v1','http://[::1]:8760/v1',
])
def test_loopback_stt_urls_never_use_system_proxies(url):
    assert trust_env_for_url(url) is False


@pytest.mark.parametrize('url',['https://speech.example/v1','http://192.168.31.9:8760/v1'])
def test_remote_stt_urls_keep_existing_proxy_policy(url):
    assert trust_env_for_url(url) is True


@pytest.fixture
def endpoints():
    servers, threads, requests = [], [], {'stt':0,'proxy':0}
    def start(kind):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get('Content-Length',0)))
                requests[kind] += 1
                payload = json.dumps({'text':'local transcription'}).encode()
                self.send_response(200 if kind == 'stt' else 502)
                self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
        server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread = threading.Thread(target=server.serve_forever,daemon=True)
        thread.start(); servers.append(server); threads.append(thread)
        return f'http://127.0.0.1:{server.server_port}'
    try:
        yield start('stt'),start('proxy'),requests
    finally:
        for server in servers:
            server.shutdown(); server.server_close()
        for thread in threads:
            thread.join(2)


def transcribe(kind,url):
    if kind == 'legacy':
        client = STTClient({'base_url':url,'model':'synthetic'})
        try:
            return client.transcribe(b'synthetic audio')
        finally:
            client.close()
    client = OpenAICompatibleTranscriber(base_url=url,model='synthetic',max_attempts=1)
    try:
        segment = AudioSegment(session_id=SessionId('synthetic'),source_id='test',generation=1,seq=1,
                               pcm=np.zeros(320,dtype=np.float32).tobytes())
        return client.transcribe(segment,[]).text
    finally:
        client.close()


@pytest.mark.parametrize('kind',['legacy','v2'])
def test_real_local_http_transcription_bypasses_a_broken_system_proxy(kind,endpoints,monkeypatch):
    endpoint, proxy, requests = endpoints
    monkeypatch.setattr(httpx._client,'get_environment_proxies',lambda:{'all://':proxy})
    assert transcribe(kind,endpoint+'/v1') == 'local transcription'
    assert requests == {'stt':1,'proxy':0}


@pytest.mark.parametrize('kind',['legacy','v2'])
def test_remote_transcription_still_uses_the_configured_proxy(kind,endpoints,monkeypatch):
    endpoint, proxy, requests = endpoints
    monkeypatch.setattr(httpx._client,'get_environment_proxies',lambda:{'all://':proxy})
    with pytest.raises(RuntimeError if kind == 'legacy' else STTServerError):
        transcribe(kind,'http://speech.example/v1')
    assert requests == {'stt':0,'proxy':1}


def test_local_connection_error_explains_the_missing_service():
    def unavailable(request):
        raise httpx.ConnectError('synthetic connection refused')
    client = STTClient({'base_url':'http://127.0.0.1:8760/v1'},transport=httpx.MockTransport(unavailable))
    try:
        with pytest.raises(RuntimeError,match='本地语音转写服务无法连接'):
            client.transcribe(b'synthetic audio')
    finally:
        client.close()
