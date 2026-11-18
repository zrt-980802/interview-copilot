"""Keep loopback requests off environment and Windows system proxies."""

import ipaddress

import httpx


def trust_env_for_url(url: str) -> bool:
    host = (httpx.URL(url).host or "").lower().rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        return False
    try:
        return not ipaddress.ip_address(host).is_loopback
    except ValueError:
        return True
