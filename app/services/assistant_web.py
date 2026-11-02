"""Bounded adapter for an explicitly configured, vetted web gateway.

The gateway must resolve/pin public destination IPs on every hop and refuse
redirects. We never fetch a model-provided URL directly from the BDR backend.
Its authenticated response must attest this contract, not merely echo the URL.
"""
from __future__ import annotations

import ipaddress
import json
import socket
from urllib.parse import urlsplit

import httpx

from app.core.config import get_settings

MAX_BYTES = 512 * 1024
POLICY = "public-https-no-redirects-v1"


class WebUnavailable(ValueError):
    def __init__(self):
        super().__init__("Web access is unavailable.")


def public_url(value: str, *, resolve: bool = True) -> str:
    from app.assistant.mail_binding import normalize_url
    safe = normalize_url(value)
    if not safe or len(safe) > 2048:
        raise WebUnavailable()
    parts = urlsplit(safe)
    host = parts.hostname or ""
    if parts.scheme != "https" or parts.port not in {None, 443} or parts.fragment or not host or host.endswith("."):
        raise WebUnavailable()
    if host in {"localhost", "metadata.google.internal"} or host.endswith((".localhost", ".local", ".internal")):
        raise WebUnavailable()
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        if not resolve:
            return safe
        try:
            addresses = [ipaddress.ip_address(r[4][0]) for r in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)]
        except (OSError, ValueError):
            raise WebUnavailable() from None
    if not addresses or any(not a.is_global or a.is_multicast or a.is_unspecified or a.is_reserved
                            for a in addresses):
        raise WebUnavailable()
    return safe


def _request(operation: str, payload: dict) -> dict:
    s = get_settings()
    if not (s.assistant_enabled and getattr(s, "assistant_web_enabled", False)
            and getattr(s, "assistant_web_api_key", "") and getattr(s, "assistant_web_api_url", "")):
        raise WebUnavailable()
    origin = public_url(s.assistant_web_api_url).rstrip("/")
    if urlsplit(origin).path not in {"", "/"} or urlsplit(origin).query:
        raise WebUnavailable()
    try:
        with httpx.Client(timeout=20, follow_redirects=False, trust_env=False) as client:
            with client.stream("POST", origin + "/" + operation,
                               headers={"Authorization": "Bearer " + s.assistant_web_api_key},
                               json={**payload, "policy": POLICY}) as response:
                if response.status_code != 200:
                    raise WebUnavailable()
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_BYTES:
                        raise WebUnavailable()
        body = json.loads(raw)
        if not isinstance(body, dict) or body.get("policy") != POLICY:
            raise WebUnavailable()
        return body
    except (httpx.HTTPError, OSError, ValueError):
        raise WebUnavailable() from None


def search(query: str, limit: int = 5, *, user_texts: list[str]) -> dict:
    if (not isinstance(query, str) or not 1 <= len(query.strip()) <= 300
            or any(ord(char) < 32 for char in query)
            or type(limit) is not int or not 1 <= limit <= 10):
        raise WebUnavailable()
    from app.assistant.mail_binding import typed_texts
    phrase = " ".join(query.split()).casefold()
    if not any(phrase in " ".join(text.split()).casefold() for text in typed_texts(user_texts)):
        raise WebUnavailable()
    body = _request("search", {"query": query.strip(), "limit": max(1, min(10, limit))})
    out = []
    rows = body.get("results")
    if not isinstance(rows, list):
        raise WebUnavailable()
    for row in rows[:max(1, min(10, limit))]:
        if not isinstance(row, dict):
            continue
        try:
            url = public_url(str(row.get("url") or ""))
        except WebUnavailable:
            continue
        out.append({"url": url, "title": str(row.get("title") or "")[:200],
                    "summary": str(row.get("summary") or "")[:1500], "citation": url})
    return {"results": out, "tainted": True}


def fetch(url: str, *, user_texts: list[str]) -> dict:
    from app.assistant.mail_binding import url_is_bound
    if not url_is_bound(url, user_texts=user_texts):
        raise WebUnavailable()
    safe = public_url(url)
    body = _request("fetch", {"url": safe, "max_bytes": MAX_BYTES, "follow_redirects": False})
    # Changed final URL or any reported hop is a refusal, never fallback.
    if body.get("url") != safe or body.get("redirects") != [] or body.get("public_destination") is not True:
        raise WebUnavailable()
    content = body.get("text")
    if not isinstance(content, str):
        raise WebUnavailable()
    return {"url": safe, "citation": safe, "title": str(body.get("title") or "")[:200],
            "text": content[:12000], "truncated": len(content) > 12000, "tainted": True}
