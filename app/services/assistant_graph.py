"""Isolated, bounded Graph transport for Phase 5 service adapters.

No credential discovery, redirects or automatic retries. The service supplies
the existing application token after validating the current BDR principal;
this transport does not decide mailbox scope or approval authority. Never
expose paths, tokens, response text or provider exception details as errors.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from app.assistant.mail_scope import normalize_address

GRAPH_ORIGIN = "https://graph.microsoft.com"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class GraphFailure(RuntimeError):
    """Sanitized provider failure, carrying only a status code if known."""

    def __init__(self, status_code: int | None = None):
        self.status_code = status_code
        super().__init__("Outlook request failed.")


class GraphSendUncertain(GraphFailure):
    """Dispatch was attempted; never automatically retry this send."""


def graph_url(path: str) -> str:
    """Accept fixed-origin v1.0 paths and validated Graph pagination URLs.

    Callers construct paths and encode individual message/mailbox identifiers.
    This deliberately rejects path normalization and all alternate origins.
    """
    if (not isinstance(path, str) or not path
            or any(ord(c) <= 32 or ord(c) >= 127 for c in path) or "\\" in path):
        raise ValueError("Invalid Outlook resource.")
    url = GRAPH_ORIGIN + path if path.startswith("/v1.0/") else path
    parts = urlsplit(url)
    if (parts.scheme != "https" or parts.netloc != "graph.microsoft.com"
            or not parts.path.startswith("/v1.0/") or parts.fragment
            or any(segment in {".", ".."} for segment in parts.path.split("/"))
            or "%2e" in parts.path.casefold()):
        raise ValueError("Invalid Outlook resource.")
    return url


class GraphTransport:
    """One explicit identity per instance. Tokens stay out of repr/errors.

    Construct after the caller validates current profile and mailbox scope.
    Reader services expose read_json only; mail sends require their separate
    durable approval dispatch claim before invoking send_mail. No retries
    exist here, including on 429, 503 or connection failures.
    """

    def __init__(self, access_token: str, *, reader: bool = False, sender_mailbox: str | None = None,
                 transport: httpx.BaseTransport | None = None):
        if (not isinstance(access_token, str) or not access_token
                or any(ord(c) <= 32 or ord(c) >= 127 for c in access_token)):
            raise ValueError("Outlook credentials unavailable.")
        self._token = access_token
        self._reader = reader
        self._sender = normalize_address(sender_mailbox) if sender_mailbox is not None else None
        if sender_mailbox is not None and self._sender is None:
            raise ValueError("Invalid Outlook sender.")
        self._transport = transport

    def _request(self, method: str, path: str, *, payload: dict | None = None,
                 params: dict | None = None, max_bytes: int = MAX_RESPONSE_BYTES,
                 prefer: str | None = None) -> tuple[int, bytes]:
        url = graph_url(path)
        if not 1 <= max_bytes <= 20 * 1024 * 1024:
            raise ValueError("Invalid Outlook response limit.")
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        if prefer is not None:
            if prefer not in {'outlook.body-content-type="text"',
                              'outlook.timezone="Pacific Standard Time"'}:
                raise ValueError("Invalid Outlook preference.")
            headers["Prefer"] = prefer
        try:
            with httpx.Client(transport=self._transport, timeout=httpx.Timeout(30, connect=10),
                              follow_redirects=False, trust_env=False) as client:
                with client.stream(method, url, headers=headers, params=params, json=payload) as response:
                    status = response.status_code
                    # Do not ingest error bodies (which may echo mailbox data).
                    if not 200 <= status < 300:
                        raise GraphFailure(status)
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > max_bytes:
                            raise GraphFailure(status)
                    return status, bytes(body)
        except (httpx.HTTPError, OSError):
            raise GraphFailure() from None

    def read_json(self, path: str, *, params: dict | None = None,
                  max_bytes: int = MAX_RESPONSE_BYTES, prefer: str | None = None) -> dict[str, Any]:
        if not 1 <= max_bytes <= MAX_RESPONSE_BYTES:
            raise ValueError("Invalid Outlook response limit.")
        _status, raw = self._request("GET", path, params=params, max_bytes=max_bytes, prefer=prefer)
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise GraphFailure() from None
        if not isinstance(result, dict):
            raise GraphFailure()
        return result

    def read_bytes(self, path: str, *, max_bytes: int) -> bytes:
        """Bounded fileAttachment $value bytes, with the same no-redirect rule."""
        if not urlsplit(graph_url(path)).path.endswith("/$value"):
            raise ValueError("Invalid Outlook attachment resource.")
        _status, raw = self._request("GET", path, max_bytes=max_bytes)
        return raw

    def send_mail(self, payload: dict) -> None:
        """Dispatch once to the server-bound /users/{sender}/sendMail endpoint.

        The caller freezes/validates the complete payload and owns its ledger.
        Any error after dispatch starts is conservatively uncertain, including
        error statuses, so no exception can cause automatic send replay.
        """
        if self._reader:
            raise ValueError("Reader identity cannot send mail.")
        if self._sender is None:
            raise ValueError("Server-bound sender required.")
        if not isinstance(payload, dict) or payload.get("saveToSentItems") is not True:
            raise ValueError("Sent Items record required.")
        message = payload.get("message")
        if not isinstance(message, dict) or any(key.casefold() in {"from", "sender"} for key in message):
            raise ValueError("Sender must come from the BDR profile.")
        try:
            path = f"/v1.0/users/{quote(self._sender, safe='@')}/sendMail"
            status, _raw = self._request("POST", path, payload=payload)
            if status != 202:
                raise GraphFailure(status)
        except GraphFailure as error:
            raise GraphSendUncertain(error.status_code) from None
