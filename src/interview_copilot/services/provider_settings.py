"""Local provider overrides with user-scoped encrypted credentials."""

from contextlib import closing
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
import asyncio
import ctypes
import json
import os
import sqlite3
import sys
import threading
import time
from urllib.parse import urlsplit

import httpx


class ProviderSettingsError(ValueError):
    pass


class WindowsCredentialProtection:
    def _crypt(self, data, decrypt=False):
        if sys.platform != "win32":
            raise ProviderSettingsError("本机凭据加密目前需要 Windows 用户环境")
        from ctypes import wintypes
        class Blob(ctypes.Structure):
            _fields_ = [("size",wintypes.DWORD),("data",ctypes.POINTER(ctypes.c_ubyte))]
        buffer = ctypes.create_string_buffer(data)
        source = Blob(len(data),ctypes.cast(buffer,ctypes.POINTER(ctypes.c_ubyte)))
        destination = Blob()
        crypt = ctypes.WinDLL("crypt32",use_last_error=True)
        kernel = ctypes.WinDLL("kernel32",use_last_error=True)
        function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
        function.argtypes = [ctypes.POINTER(Blob),ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,
                             ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(Blob)]
        function.restype = wintypes.BOOL
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        if not function(ctypes.byref(source),None,None,None,None,1,ctypes.byref(destination)):
            raise ProviderSettingsError("本机凭据加密或解密失败，请重新输入密钥")
        try:
            return ctypes.string_at(destination.data,destination.size)
        finally:
            kernel.LocalFree(destination.data)

    def protect(self, value):
        return self._crypt(value.encode("utf-8"))

    def unprotect(self, value):
        return self._crypt(value,True).decode("utf-8")


@dataclass
class ProviderChange:
    revision: int
    config: dict
    overrides: dict
    credentials: dict = field(repr=False)
    persisted: dict = field(repr=False)


class ProviderSettings:
    FIELDS = {"base_url","quick_model","detail_model","allow_anonymous","fallback_enabled",
              "fallback_base_url","fallback_model","fallback_allow_anonymous"}
    def __init__(self,path,defaults,*,protection=None):
        self.path, self.defaults = Path(path),deepcopy(defaults)
        self.protection = protection or WindowsCredentialProtection()
        self._lock = threading.RLock()
        self._overrides, self._keys, self._persisted, self._errors = {},{}, {},set()
        self.revision = 0

    def _conn(self):
        return sqlite3.connect(self.path,timeout=5)

    def start(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self._lock, closing(self._conn()) as conn, conn:
            conn.execute("CREATE TABLE IF NOT EXISTS provider_settings(id INTEGER PRIMARY KEY CHECK(id=1),overrides TEXT NOT NULL,primary_key BLOB,fallback_key BLOB,revision INTEGER NOT NULL)")
            row = conn.execute("SELECT overrides,primary_key,fallback_key,revision FROM provider_settings WHERE id=1").fetchone()
            if row:
                self._overrides, self.revision = json.loads(row[0]),row[3]
                for name,blob in zip(("primary","fallback"),row[1:3]):
                    if blob is not None:
                        self._persisted[name] = blob
                        try:
                            self._keys[name] = self.protection.unprotect(blob)
                        except ProviderSettingsError:
                            self._errors.add(name)

    def _effective(self,overrides=None):
        result = {**self.defaults,**(self._overrides if overrides is None else overrides)}
        result.setdefault("allow_anonymous",False)
        result.setdefault("fallback_allow_anonymous",False)
        result.setdefault("fallback_enabled",bool(result.get("fallback_base_url") and result.get("fallback_model")
            and any(os.environ.get(name,"").strip() for name in result.get("fallback_api_key_env",[]))))
        return result

    def _credentials(self,overrides,keys):
        result = {}
        for name,prefix in (("primary",""),("fallback","fallback_")):
            if name in keys:
                result[name] = keys[name]
            elif name in self._errors or overrides.get(prefix + "web_credentials",False):
                result[name] = None
            else:
                result[name] = next((os.environ.get(env,"").strip() for env in self.defaults.get(prefix + "api_key_env",[])
                                     if os.environ.get(env,"").strip()),None)
        return result

    def snapshot(self):
        with self._lock:
            config = self._effective()
            return config,self._credentials(self._overrides,self._keys)

    def public(self):
        with self._lock:
            config,keys = self.snapshot()
            result = {name:config.get(name,False if name.endswith(("anonymous","enabled")) else "") for name in self.FIELDS}
            for name,prefix in (("primary",""),("fallback","fallback_")):
                result[prefix + "key_set"] = bool(keys[name])
                result[prefix + "key_source"] = ("unavailable" if name in self._errors else "saved" if name in self._persisted
                    else "session" if self._keys.get(name) else "environment" if keys[name] else "none")
            return {**result,"revision":self.revision,"credential_storage":"windows-dpapi"}

    @staticmethod
    def _url(value):
        if not isinstance(value,str) or len(value) > 2048:
            raise ProviderSettingsError("服务地址格式不正确")
        value = value.strip().rstrip("/")
        try:
            parsed = urlsplit(value)
            if (parsed.scheme not in {"https","http"} or not parsed.hostname or parsed.username or parsed.password
                    or parsed.query or parsed.fragment or any(ord(char) < 33 for char in value)):
                raise ValueError()
            parsed.port
        except ValueError as exc:
            raise ProviderSettingsError("服务地址须为 HTTP/HTTPS URL，不能包含账号、查询参数或密钥") from exc
        return value

    def prepare(self,payload,*,require_models=True):
        allowed = self.FIELDS | {"api_key","fallback_api_key","clear_api_key","clear_fallback_api_key","remember","revision"}
        if not isinstance(payload,dict):
            raise ProviderSettingsError("配置须为对象")
        if set(payload) - allowed:
            raise ProviderSettingsError("包含不支持的配置字段")
        for name in ("remember","clear_api_key","clear_fallback_api_key"):
            if name in payload and type(payload[name]) is not bool:
                raise ProviderSettingsError("开关值不正确")
        with self._lock:
            if "revision" in payload and type(payload["revision"]) is not int:
                raise ProviderSettingsError("配置版本不正确")
            if payload.get("revision",self.revision) != self.revision:
                raise ProviderSettingsError("配置已在其他页面更新，请刷新后再保存")
            overrides,keys,persisted = deepcopy(self._overrides),dict(self._keys),dict(self._persisted)
            for name in self.FIELDS.intersection(payload):
                value = payload[name]
                if name.endswith(("anonymous","enabled")):
                    if type(value) is not bool:
                        raise ProviderSettingsError("开关值不正确")
                elif not isinstance(value,str):
                    raise ProviderSettingsError("服务地址和模型须为文本")
                else:
                    value = value.strip()
                overrides[name] = value
            config = self._effective(overrides)
            config["base_url"] = overrides["base_url"] = self._url(config.get("base_url",""))
            if config["fallback_enabled"]:
                config["fallback_base_url"] = overrides["fallback_base_url"] = self._url(config.get("fallback_base_url",""))
            for name in ("quick_model","detail_model","fallback_model"):
                value = config.get(name,"")
                if (not isinstance(value,str) or len(value) > 200 or any(ord(char) < 32 for char in value)
                        or (require_models and not value and (name != "fallback_model" or config["fallback_enabled"]))):
                    raise ProviderSettingsError("请填写有效的快速/深入模型名称，启用备用时也需指定备用模型")
            for name,prefix in (("primary",""),("fallback","fallback_")):
                key_field,clear_field = prefix + "api_key","clear_" + prefix + "api_key"
                value = payload.get(key_field,"")
                if not isinstance(value,str) or len(value)>8192 or any(ord(char)<32 for char in value):
                    raise ProviderSettingsError("API Key 格式不正确")
                value = value.strip()
                changed_url = config.get(prefix + "base_url","").rstrip("/") != self._effective().get(prefix + "base_url","").rstrip("/")
                if payload.get(clear_field,False) or changed_url:
                    keys[name] = None; persisted.pop(name,None); overrides[prefix + "web_credentials"] = True
                if value:
                    keys[name] = value; overrides[prefix + "web_credentials"] = True
                    if payload.get("remember",True):
                        persisted[name] = self.protection.protect(value)
                    else:
                        persisted.pop(name,None)
            return ProviderChange(self.revision,config,overrides,self._credentials(overrides,keys),persisted)

    def save(self,change):
        with self._lock, closing(self._conn()) as conn, conn:
            if change.revision != self.revision:
                raise ProviderSettingsError("配置已更新，请刷新后再保存")
            conn.execute("INSERT OR REPLACE INTO provider_settings VALUES(1,?,?,?,?)",
                (json.dumps(change.overrides,ensure_ascii=False),change.persisted.get("primary"),change.persisted.get("fallback"),self.revision+1))
            conn.commit()
            self._overrides,self._persisted = change.overrides,change.persisted
            self._keys = {name:value for name,value in change.credentials.items() if change.overrides.get(("" if name=="primary" else "fallback_") + "web_credentials",False)}
            self._errors = {name for name in self._errors if name in self._persisted and not change.credentials.get(name)}
            self.revision += 1
        return self.public()


async def probe_provider(change,*,channel="primary",models=False,transport=None):
    if channel not in {"primary","fallback"}:
        raise ProviderSettingsError("服务类型不正确")
    prefix = "" if channel == "primary" else "fallback_"
    config = change.config
    if channel == "fallback" and not config.get("fallback_enabled"):
        raise ProviderSettingsError("备用服务未启用")
    key = change.credentials.get(channel)
    if not key and not config.get(prefix + "allow_anonymous"):
        raise ProviderSettingsError("请填写 API Key，或明确选择无需认证")
    headers = {"Authorization":"Bearer " + key} if key else {}
    base, started = config[prefix + "base_url"],time.monotonic()
    try:
        async with asyncio.timeout(30), httpx.AsyncClient(timeout=20,follow_redirects=False,transport=transport) as client:
            if models:
                response = await client.get(base + "/models",headers=headers)
            else:
                response = await client.post(base + "/chat/completions",headers=headers,json={
                    "model":config["quick_model" if channel=="primary" else "fallback_model"],
                    "messages":[{"role":"user","content":"Reply with OK."}],"max_tokens":16,"stream":False,
                    **({name:value for name,value in config.get("provider_options",{}).items()
                        if name in {"thinking","enable_thinking","reasoning_effort"}} if channel == "primary" else {})})
            if response.status_code != 200:
                raise ProviderSettingsError(f"服务返回 HTTP {response.status_code}，请检查地址、密钥和模型权限")
            value = response.json()
            if not isinstance(value,dict):
                raise ProviderSettingsError("服务响应格式不正确")
            if models:
                if not isinstance(value.get("data"),list):
                    raise ProviderSettingsError("服务没有返回模型列表")
                found = sorted({item["id"] for item in value.get("data",[]) if isinstance(item,dict)
                                and isinstance(item.get("id"),str) and 0<len(item["id"])<=200})[:500]
                return {"ok":True,"models":found}
            choices = value.get("choices")
            if (not isinstance(choices,list) or not choices or not isinstance(choices[0],dict)
                    or not isinstance(choices[0].get("message"),dict)
                    or not isinstance(choices[0]["message"].get("content"),str)
                    or not choices[0]["message"]["content"].strip()):
                raise ProviderSettingsError("服务没有返回有效的回答，请确认是 OpenAI 兼容接口")
            return {"ok":True,"elapsed_ms":round((time.monotonic()-started)*1000)}
    except (httpx.HTTPError,TimeoutError,ValueError,TypeError) as exc:
        if isinstance(exc,ProviderSettingsError):
            raise
        raise ProviderSettingsError("连接失败或响应格式不正确，请检查服务与网络") from exc
