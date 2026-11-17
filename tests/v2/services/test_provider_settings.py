import asyncio
from contextlib import closing
import json
import sqlite3
import sys

import httpx
import pytest

from interview_copilot.services.provider_settings import ProviderSettings, ProviderSettingsError, WindowsCredentialProtection, probe_provider


DEFAULTS = {"base_url":"https://primary.invalid/v1","quick_model":"quick","detail_model":"detail",
            "api_key_env":["SYNTHETIC_PROVIDER_KEY"],"fallback_base_url":"https://fallback.invalid/v1",
            "fallback_model":"backup","fallback_api_key_env":["SYNTHETIC_FALLBACK_KEY"]}


@pytest.fixture
def store(tmp_path,monkeypatch):
    monkeypatch.delenv("SYNTHETIC_PROVIDER_KEY",raising=False)
    monkeypatch.delenv("SYNTHETIC_FALLBACK_KEY",raising=False)
    value = ProviderSettings(tmp_path / "providers.db",DEFAULTS)
    value.start()
    return value


@pytest.mark.skipif(sys.platform != "win32",reason="Windows DPAPI")
def test_dpapi_credentials_survive_restart_without_plaintext(store):
    secret = "synthetic-provider-secret-123"
    store.save(store.prepare({"api_key":secret}))
    with closing(sqlite3.connect(store.path)) as conn:
        blob = conn.execute("SELECT primary_key FROM provider_settings").fetchone()[0]
    assert isinstance(blob,bytes) and secret.encode() not in blob
    assert secret.encode() not in store.path.read_bytes()
    assert secret not in json.dumps(store.public())
    restarted = ProviderSettings(store.path,DEFAULTS)
    restarted.start()
    assert restarted.snapshot()[1]["primary"] == secret
    assert restarted.public()["key_source"] == "saved"


def test_session_key_and_clear_prevent_environment_fallback(store,monkeypatch):
    monkeypatch.setenv("SYNTHETIC_PROVIDER_KEY","synthetic-environment-key")
    assert store.public()["key_source"] == "environment"
    store.save(store.prepare({"api_key":"session-secret","remember":False}))
    assert store.public()["key_source"] == "session"
    restarted = ProviderSettings(store.path,DEFAULTS)
    restarted.start()
    assert restarted.snapshot()[1]["primary"] is None
    assert not restarted.public()["key_set"]
    store.save(store.prepare({"clear_api_key":True}))
    assert store.snapshot()[1]["primary"] is None
    assert store.public()["key_source"] == "none"


@pytest.mark.skipif(sys.platform != "win32",reason="Windows DPAPI")
def test_replacing_saved_key_with_session_key_removes_old_persisted_key(store):
    store.save(store.prepare({"api_key":"old-saved-key"}))
    store.save(store.prepare({"api_key":"new-session-key","remember":False}))
    assert store.snapshot()[1]["primary"] == "new-session-key"
    restarted = ProviderSettings(store.path,DEFAULTS)
    restarted.start()
    assert restarted.snapshot()[1]["primary"] is None
    assert restarted.public()["key_source"] == "none"


def test_switching_endpoint_does_not_send_original_key(store,monkeypatch):
    monkeypatch.setenv("SYNTHETIC_PROVIDER_KEY","old-environment-key")
    change = store.prepare({"base_url":"https://new.invalid/v1"})
    assert change.credentials["primary"] is None
    store.save(change)
    assert store.snapshot()[1]["primary"] is None
    store.save(store.prepare({"api_key":"replacement","remember":False}))
    assert store.snapshot()[1]["primary"] == "replacement"


@pytest.mark.parametrize("payload",[
    {"remember":"false"},{"clear_api_key":"true"},{"revision":True},{"allow_anonymous":1},
    {"base_url":"https://user:secret@provider.invalid/v1"},
    {"base_url":"https://provider.invalid/v1?key=secret"},{"api_key":"key\nsecret"},
    {"quick_model":""},{"api_key_env":["OTHER_KEY"]},
])
def test_invalid_configuration_is_rejected(store,payload):
    with pytest.raises(ProviderSettingsError):
        store.prepare(payload)


def test_revision_conflict_does_not_overwrite(store):
    first,second = store.prepare({"quick_model":"first"}),store.prepare({"quick_model":"second"})
    store.save(first)
    with pytest.raises(ProviderSettingsError,match="更新"):
        store.save(second)
    assert store.public()["quick_model"] == "first"


def test_unreadable_key_remains_unavailable_after_unrelated_save(store):
    with closing(sqlite3.connect(store.path)) as conn,conn:
        conn.execute("INSERT INTO provider_settings VALUES(1,'{}',?,NULL,1)",(b"not-a-dpapi-blob",))
    store.start()
    assert store.public()["key_source"] == "unavailable"
    store.save(store.prepare({"quick_model":"new-model"}))
    assert store.public()["key_source"] == "unavailable"
    assert not store.public()["key_set"]


def test_probe_uses_only_selected_credentials_and_synthetic_message(store):
    change = store.prepare({"api_key":"primary-test","fallback_api_key":"backup-test","remember":False,"fallback_enabled":True})
    seen = []
    def respond(request):
        seen.append(request)
        if request.method == "GET":
            return httpx.Response(200,json={"data":[{"id":"model-b"},{"id":"model-a"},{"id":"model-a"}]})
        return httpx.Response(200,json={"choices":[{"message":{"content":"OK"}}]})
    transport = httpx.MockTransport(respond)
    assert asyncio.run(probe_provider(change,channel="fallback",transport=transport))["ok"]
    assert str(seen[0].url) == "https://fallback.invalid/v1/chat/completions"
    assert seen[0].headers["authorization"] == "Bearer backup-test"
    assert json.loads(seen[0].content)["messages"] == [{"role":"user","content":"Reply with OK."}]
    assert asyncio.run(probe_provider(change,models=True,transport=transport))["models"] == ["model-a","model-b"]
    assert store.revision == 0


@pytest.mark.parametrize("response",[httpx.Response(401,text="private-key-in-error"),httpx.Response(200,json=[]),
    httpx.Response(200,json={"choices":[]}),httpx.Response(200,json={"choices":[{"message":{}}]})])
def test_probe_errors_do_not_echo_provider_body(store,response):
    change = store.prepare({"allow_anonymous":True})
    with pytest.raises(ProviderSettingsError) as error:
        asyncio.run(probe_provider(change,transport=httpx.MockTransport(lambda request:response)))
    assert "private-key" not in str(error.value)


def test_anonymous_probe_does_not_send_authorization(store):
    seen = []
    def respond(request):
        seen.append(request)
        return httpx.Response(200,json={"choices":[{"message":{"content":"OK"}}]})
    asyncio.run(probe_provider(store.prepare({"allow_anonymous":True}),transport=httpx.MockTransport(respond)))
    assert "authorization" not in seen[0].headers
