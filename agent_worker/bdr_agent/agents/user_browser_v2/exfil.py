"""Exfiltration rules for user_browser v2 (plan 10.3, WI-9). Signed worker code.

Model-controlled data enters the browser in exactly two ways: the strings the
model types (type_text, select_option) and the URLs it asks to open
(navigate). These rules keep that data on the task's start origin.

  Typed-string rule (`TypedStrings.found_in`): any request to a host other
    than the start origin whose URL or body carries a typed string of 12 or
    more characters (raw, case-folded, URL-encoded once or twice, JSON
    escaped, or base64 at any byte alignment, standard or URL-safe) is
    refused. Long typed strings are also checked as overlapping windows, so a
    page that forwards part of a long comment is caught too. The guard applies
    this to every request, frame, redirect hop and socket message.

  Navigate URL rule (`check_navigate_url`): a URL the model asks to open is at
    most 2048 characters, no query value over 256 characters and no base64 or
    hex run over 64 characters in the query or fragment, unless the exact URL
    is a link on the current page or the task's start URL. Applies to every
    host, in scope or not (an out-of-scope URL is then site-held, 5.4).

  Off-site query budget (`QueryBudget`): the query bytes of top-level
    navigations to hosts other than the start origin (model navigations, link
    clicks and GET form submissions; never redirect hops, which carry the
    server's data, not the model's) count against the job policy cap
    (`caps.offsite_query_bytes`, default 4096). Past it such navigations are
    refused.

Also here, as signed constants the server cannot widen: the analytics hosts
whose requests are dropped (6.2), the Chromium launch flags for WebRTC, DNS
prefetch and shared workers (10.3; WI-8 adds them to the per-job launch), and
the init script that removes WebRTC, SharedWorker, WebSocketStream and
WebTransport from every frame, and the prelude that removes every socket API
from every dedicated worker.

`trace_url` is the 10.7 shape for any stored or traced URL: origin and path
only, token-looking path segments replaced with `:token`.
"""

from __future__ import annotations

import base64
import json
import re
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import parse_qsl, quote, quote_plus, unquote, unquote_plus, urlsplit, urlunsplit

from ...protocol import URL_MAX, USER_TYPED_STRING_MIN_CHARS, host_matches, url_origin

TYPED_MIN = USER_TYPED_STRING_MIN_CHARS          # 12 (plan 10.3)
TYPED_WINDOW = 24                               # window length for long strings
TYPED_STRIDE = 12                               # any 36-char run of a long string contains a whole window
TYPED_MAX_STRINGS = 1000                        # per job; oldest dropped first
SCAN_MAX_BYTES = 8 * 1024 * 1024                # body bytes scanned per request
NAV_URL_MAX = URL_MAX                           # 2048
NAV_QUERY_VALUE_MAX = 256
NAV_ENCODED_RUN_MAX = 64
_ENCODED_RUN = re.compile(r"[A-Za-z0-9+/_-]{%d,}" % (NAV_ENCODED_RUN_MAX + 1))
_JSON_UNICODE = re.compile(r"\\u([0-9a-fA-F]{4})")

# Dropped silently (aborted, no card): not user actions, and dropping them
# does not break pages (6.2). Signed code; the server cannot add or remove.
ANALYTICS_HOSTS: tuple[str, ...] = (
    "google-analytics.com", "*.google-analytics.com", "analytics.google.com", "*.analytics.google.com",
    "stats.g.doubleclick.net", "*.doubleclick.net", "www.googletagmanager.com", "googletagmanager.com",
    "*.hotjar.com", "*.hotjar.io", "api.segment.io", "cdn.segment.com", "*.segment.com",
    "api.mixpanel.com", "*.mixpanel.com", "api.amplitude.com", "*.amplitude.com", "api2.amplitude.com",
    "*.fullstory.com", "heapanalytics.com", "*.heapanalytics.com", "*.heap.io",
    "bam.nr-data.net", "*.nr-data.net", "js-agent.newrelic.com",
    "*.ingest.sentry.io", "*.sentry.io", "browser-intake-datadoghq.com", "*.browser-intake-datadoghq.com",
    "*.datadoghq.com", "events.launchdarkly.com", "*.clarity.ms", "bat.bing.com", "px.ads.linkedin.com",
    "*.pendo.io", "app.pendo.io", "cdn.pendo.io", "*.quantserve.com", "*.scorecardresearch.com",
)

# Request resource types that are never user actions: dropped (6.2).
DROP_RESOURCE_TYPES = frozenset({"ping", "beacon", "cspreport", "csp_report", "csp-report"})

# Per-job Chromium flags (10.3). WI-8 appends these to the user-job launch.
# Note: Chromium honours only the last --disable-blink-features flag, so a
# launch that needs another blink feature off must merge it into this value.
EXFIL_LAUNCH_ARGS: tuple[str, ...] = (
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--dns-prefetch-disable",
    "--disable-blink-features=SharedWorker",
)
# Network prediction (preconnect) feature names are checked against the
# shipped Chromium in WI-11 and added there (plan 10.3 DNS row); with a fixed
# proxy Chromium does not resolve proxied hosts itself.

# Worker scopes (review WI-9). route_web_socket hooks page frames only: a
# dedicated Worker's global scope has its own WebSocket that neither
# context.route nor route_web_socket sees. So every Worker of a user job runs
# WORKER_SCOPE_JS (with isWorker true) before its first line, and that removes
# every socket API from the worker scope. The same function, run in every
# frame by EXFIL_INIT_SCRIPT, replaces Worker with a wrapper:
#   - blob: script: read synchronously (before the page can revoke it), copied
#     to a new blob, and started from a bootstrap blob that runs the prelude,
#     then importScripts(copy) (classic) or await import(copy) (module);
#   - http(s) script: the URL gets the query marker WORKER_MARKER=classic|module;
#     the guard answers that request with worker_bootstrap(), which runs the
#     prelude, then loads the real script (importScripts or dynamic import), so
#     the worker keeps its real directory as base URL;
#   - anything else (data:, ...) is refused.
# Nested workers get the same wrapper. Every intrinsic the wrapper uses is
# captured before page scripts run, so a page cannot steer it by patching URL,
# Blob, XMLHttpRequest or String methods; Worker.prototype.constructor is the
# wrapper too. A failure throws: no worker starts without the prelude.
WORKER_MARKER = "__bdr_worker"
WORKER_SCOPE_JS = r"""function bdrInstallScope(scope, isWorker) {
  "use strict";
  const MARK = "__bdr_worker";
  const R = Reflect, apply = R.apply, define = Object.defineProperty;
  const remove = (name) => {
    try { delete scope[name]; } catch (e) {}
    try { define(scope, name, {value: undefined, configurable: false, writable: false}); } catch (e) {}
  };
  const Native = scope.Worker;
  if (typeof Native === "function") {
    const SRC = "(" + bdrInstallScope.toString() + ")(self, true);\n";
    const U = scope.URL, B = scope.Blob, X = scope.XMLHttpRequest, DE = scope.DOMException, TE = TypeError;
    const stringify = JSON.stringify, Str = String, indexOf = Str.prototype.indexOf, slice = Str.prototype.slice;
    const createURL = U.createObjectURL;
    const hrefGet = R.getOwnPropertyDescriptor(U.prototype, "href").get;
    const protoGet = R.getOwnPropertyDescriptor(U.prototype, "protocol").get;
    const baseGet = isWorker ? R.getOwnPropertyDescriptor(scope.WorkerLocation.prototype, "href").get
                             : R.getOwnPropertyDescriptor(scope.Node.prototype, "baseURI").get;
    const baseThis = isWorker ? scope.location : scope.document;
    const xOpen = X.prototype.open, xSend = X.prototype.send;
    const xText = R.getOwnPropertyDescriptor(X.prototype, "responseText").get;
    const xStatus = R.getOwnPropertyDescriptor(X.prototype, "status").get;
    const blobURL = (text) => apply(createURL, U, [new B([text], {type: "text/javascript"})]);
    const refuse = (why) => new DE("Worker refused by the assistant browser: " + why, "SecurityError");
    const rewrite = (url, module) => {
      const abs = new U(Str(url), apply(baseGet, baseThis, []));
      const href = apply(hrefGet, abs, []), proto = apply(protoGet, abs, []);
      if (proto === "blob:") {
        const x = new X();
        apply(xOpen, x, ["GET", href, false]);
        apply(xSend, x, []);
        const status = apply(xStatus, x, []);
        if (status !== 200 && status !== 0) throw refuse("blob unreadable");
        const copy = blobURL(apply(xText, x, []));
        return blobURL(SRC + (module ? "await import(" + stringify(copy) + ");\n"
                                     : "importScripts(" + stringify(copy) + ");\n"));
      }
      if (proto === "http:" || proto === "https:") {
        const cut = apply(indexOf, href, ["#"]);
        const head = cut < 0 ? href : apply(slice, href, [0, cut]);
        const sep = apply(indexOf, head, ["?"]) < 0 ? "?" : "&";
        return head + sep + MARK + "=" + (module ? "module" : "classic");
      }
      throw refuse("scheme " + proto);
    };
    const Wrapper = function Worker(url, options) {
      if (new.target === undefined) throw new TE("Failed to construct 'Worker': Please use the 'new' operator.");
      const o = options === undefined || options === null ? {} : options;
      const type = o.type === undefined ? "classic" : Str(o.type);
      const opts = {type: type};
      if (o.name !== undefined) opts.name = Str(o.name);
      if (o.credentials !== undefined) opts.credentials = Str(o.credentials);
      return R.construct(Native, [rewrite(url, type === "module"), opts], Native);
    };
    try { define(Wrapper, "prototype", {value: Native.prototype, writable: false, configurable: false}); } catch (e) {}
    try { define(Native.prototype, "constructor", {value: Wrapper, writable: false, configurable: false}); } catch (e) {}
    try { delete scope.Worker; } catch (e) {}
    try { define(scope, "Worker", {value: Wrapper, configurable: false, writable: false}); } catch (e) {}
  }
  if (isWorker) {
    remove("WebSocket"); remove("WebSocketStream"); remove("WebTransport");
    remove("RTCPeerConnection"); remove("RTCDataChannel"); remove("SharedWorker");
  }
}"""


def split_worker_marker(url: str) -> tuple[str | None, str]:
    """(kind, url without the marker) for a URL the Worker wrapper marked;
    (None, url) otherwise. kind is "classic" or "module"."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None, url
    if WORKER_MARKER not in parts.query:
        return None, url
    kind = None
    kept: list[str] = []
    for piece in parts.query.split("&"):
        key, _, value = piece.partition("=")
        if key == WORKER_MARKER:
            kind = value if value in ("classic", "module") else (kind or "classic")
            continue
        kept.append(piece)
    if kind is None:
        return None, url
    return kind, urlunsplit((parts.scheme, parts.netloc, parts.path, "&".join(kept), parts.fragment))


def worker_bootstrap(kind: str, url: str) -> str:
    """The script the guard serves for a marked worker URL: the prelude, then
    the real script."""
    load = (f"await import({json.dumps(url)});" if kind == "module" else f"importScripts({json.dumps(url)});")
    return f"({WORKER_SCOPE_JS})(self, true);\n{load}\n"


# Installed in every frame of a user job (context.add_init_script). Removed:
#   - WebRTC (10.3);
#   - SharedWorker: blocked by design for user jobs (plan 6.2 item 2; also off
#     by --disable-blink-features=SharedWorker), so whether Playwright routes
#     its requests does not matter;
#   - WebSocketStream and WebTransport: route_web_socket replaces only the
#     WebSocket class, so these would open sockets past the websocket_ok rule
#     and the typed-string check (review WI-9).
# Worker is replaced by the wrapper above (WORKER_SCOPE_JS).
EXFIL_INIT_SCRIPT = r"""
(() => {
  const names = ["RTCPeerConnection", "webkitRTCPeerConnection", "RTCDataChannel",
                 "RTCSessionDescription", "RTCIceCandidate", "SharedWorker",
                 "WebSocketStream", "WebTransport"];
  for (const name of names) {
    try { delete window[name]; } catch (e) {}
    try {
      Object.defineProperty(window, name, {value: undefined, configurable: false, writable: false});
    } catch (e) {}
  }
})();
(""" + WORKER_SCOPE_JS + r""")(window, false);
"""


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str | None = None

    def __bool__(self) -> bool:
        return self.ok


ALLOW = Verdict(True)


def is_analytics_host(host: str | None) -> bool:
    return host_matches(host or "", ANALYTICS_HOSTS)


def is_offsite(url: str, start_origin: str) -> bool:
    """True when `url` is not on the task's start origin (unparseable counts as off-site)."""
    return url_origin(url) != start_origin


# Typed strings ---------------------------------------------------------------------


def _b64_cores(data: bytes) -> set[str]:
    """Base64 text that encodes `data` wherever it sits in a longer byte
    string: one core per alignment, with the alignment-dependent edge
    characters removed. Standard and URL-safe alphabets."""
    out: set[str] = set()
    for offset, lead in ((0, 0), (1, 2), (2, 3)):
        padded = b"\0" * offset + data
        whole = (len(padded) // 3) * 3
        core = base64.b64encode(padded[:whole]).decode("ascii")[lead:]
        if len(core) >= TYPED_MIN:
            out.add(core)
            out.add(core.replace("+", "-").replace("/", "_"))
    return out


def _windows(text: str) -> list[str]:
    if len(text) <= TYPED_WINDOW + TYPED_STRIDE:
        return [text]
    out = [text[i:i + TYPED_WINDOW] for i in range(0, len(text) - TYPED_WINDOW + 1, TYPED_STRIDE)]
    out.append(text[-TYPED_WINDOW:])
    return out


class TypedStrings:
    """The strings the model typed in this job (type_text, select_option).

    `add` is called by the tool handlers (WI-10) with the exact text typed or
    the option chosen. Every string is kept for `is_typed` (hold summaries
    show a field value only when it equals one, 6.5). Strings of 12 or more
    characters also feed the exfiltration check."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._all: list[str] = []
        self._exact: set[str] = set()
        self._plain: set[str] = set()       # case-folded raw needles
        self._encoded: set[str] = set()     # case-sensitive encoded needles (base64)

    def add(self, text: str | None) -> None:
        if not isinstance(text, str):
            return
        value = text.strip()
        if not value:
            return
        with self._lock:
            if value in self._exact:
                return
            self._all.append(value)
            self._exact.add(value)
            if len(self._all) > TYPED_MAX_STRINGS:
                self._all.pop(0)
            self._rebuild_locked()

    def _rebuild_locked(self) -> None:
        self._exact = set(self._all)
        plain: set[str] = set()
        encoded: set[str] = set()
        for value in self._all:
            if len(value) < TYPED_MIN:
                continue
            for win in _windows(value):
                plain.add(win.casefold())
                encoded |= _b64_cores(win.encode("utf-8"))
                # Encodings that survive one decode pass of the haystack are
                # covered by decoding; these cover a page that double-encodes.
                plain.add(quote(win, safe="").casefold())
                plain.add(quote_plus(win).casefold())
        self._plain = {p for p in plain if len(p) >= TYPED_MIN}
        self._encoded = encoded

    def is_typed(self, value: str | None) -> bool:
        """Exact match with a typed string (any length)."""
        if not isinstance(value, str):
            return False
        with self._lock:
            return value.strip() in self._exact

    @property
    def armed(self) -> bool:
        """True when at least one string long enough for the exfiltration rule exists."""
        with self._lock:
            return bool(self._plain or self._encoded)

    def found_in(self, url: str, body: bytes | str | None = None) -> bool:
        """Does `url` or `body` carry a typed string of 12 or more characters?"""
        with self._lock:
            plain, encoded = self._plain, self._encoded
        if not plain and not encoded:
            return False
        for hay in _haystacks(url, body):
            folded = hay.casefold()
            if any(n in folded for n in plain):
                return True
            if any(n in hay for n in encoded):
                return True
        return False

    def clear(self) -> None:
        with self._lock:
            self._all.clear()
            self._rebuild_locked()


def _json_unescape(text: str) -> str:
    if "\\" not in text:
        return text
    text = text.replace("\\/", "/").replace('\\"', '"')
    return _JSON_UNICODE.sub(lambda m: chr(int(m.group(1), 16)), text)


def _haystacks(url: str, body: bytes | str | None) -> list[str]:
    raw: list[str] = [url or ""]
    if body:
        if isinstance(body, bytes):
            chunk = body[:SCAN_MAX_BYTES]
            raw.append(chunk.decode("utf-8", errors="ignore"))
            raw.append(chunk.decode("latin-1"))
        else:
            raw.append(body[:SCAN_MAX_BYTES])
    out: list[str] = []
    for text in raw:
        seen = {text}
        out.append(text)
        current = text
        for _ in range(2):
            decoded = unquote_plus(current)
            unq = unquote(current)
            for item in (decoded, unq, _json_unescape(current), _json_unescape(decoded)):
                if item not in seen:
                    seen.add(item)
                    out.append(item)
            current = decoded
    return out


# Navigate URL rule ---------------------------------------------------------------


def _canon(url: str) -> str:
    return (url or "").strip()


def check_navigate_url(url: str, *, page_links: Iterable[str] = (), start_url: str | None = None) -> Verdict:
    """The rule for a URL the model asks to open (plan 10.3 row 2)."""
    if not isinstance(url, str) or not url:
        return Verdict(False, "bad_url")
    if len(url) > NAV_URL_MAX:
        return Verdict(False, "url_too_long")
    target = _canon(url)
    if start_url and target == _canon(start_url):
        return ALLOW
    for link in page_links:
        if isinstance(link, str) and _canon(link) == target:
            return ALLOW
    try:
        parts = urlsplit(target)
    except ValueError:
        return Verdict(False, "bad_url")
    for _key, value in parse_qsl(parts.query, keep_blank_values=True):
        if len(value) > NAV_QUERY_VALUE_MAX:
            return Verdict(False, "query_value_too_long")
    for chunk in (parts.query, parts.fragment):
        if not chunk:
            continue
        for text in (chunk, unquote_plus(chunk)):
            if _ENCODED_RUN.search(text):
                return Verdict(False, "encoded_run_in_query")
    if len(unquote_plus(parts.fragment)) > NAV_QUERY_VALUE_MAX:
        return Verdict(False, "query_value_too_long")
    return ALLOW


# Off-site query budget -------------------------------------------------------------


class QueryBudget:
    """Total query bytes sent by top-level navigation to hosts other than the
    start origin (plan 10.3 row 3). `spend` returns False once the cap would
    be passed; the guard refuses that navigation."""

    def __init__(self, cap_bytes: int) -> None:
        self.cap = max(0, int(cap_bytes))
        self.spent = 0
        self._lock = threading.Lock()

    def set_cap(self, cap_bytes: int) -> None:
        with self._lock:
            self.cap = max(0, int(cap_bytes))

    def spend(self, url: str) -> bool:
        try:
            query = urlsplit(url).query
        except ValueError:
            return False
        size = len(query.encode("utf-8"))
        if size == 0:
            return True
        with self._lock:
            if self.spent + size > self.cap:
                return False
            self.spent += size
            return True


# Traces (10.7) ---------------------------------------------------------------------

_TOKENISH = re.compile(r"^(?:[A-Za-z0-9_-]{20,}|[0-9a-fA-F]{20,})$")


def trace_url(url: str) -> str:
    """Origin plus path only; query and fragment dropped; path segments that
    look like tokens (20 or more base64url or hex characters) become :token."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return "[bad_url]"
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    if scheme not in ("http", "https", "ws", "wss") or not host:
        return f"[{scheme or 'no'}_url]"
    origin = f"{scheme}://{host}" + (f":{port}" if port else "")
    segments = [":token" if _TOKENISH.match(seg) else seg for seg in (parts.path or "/").split("/")]
    path = "/".join(segments)[:512]
    return origin + (path if path.startswith("/") else "/" + path)
