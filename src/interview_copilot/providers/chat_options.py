"""Provider options shared by answer requests and connection checks."""

from urllib.parse import urlsplit


def chat_request_options(config, channel="primary"):
    prefix = "fallback_" if channel == "fallback" else ""
    host = urlsplit(config.get(prefix + "base_url", "")).hostname or ""
    extra = {}
    if config.get("disable_thinking", True) and (host == "bigmodel.cn" or host.endswith(".bigmodel.cn")):
        extra["thinking"] = {"type": "disabled"}
    if channel == "primary":
        extra.update({name: value for name, value in config.get("provider_options", {}).items()
                      if name in {"thinking", "enable_thinking", "reasoning_effort"}})
    return extra
