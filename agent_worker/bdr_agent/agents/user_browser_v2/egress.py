"""Egress floor for user_browser jobs (docs/ASSISTANT_BROWSER_V2_PLAN.md 10.1
layer 1, 10.2, 10.6). Signed worker code: the server cannot widen it.

A small HTTP proxy that runs in its OWN process (one per job, started and
supervised by the worker), so TLS piping, 450 MB downloads and resolver stalls
never block the worker's event loop, heartbeats or leases. Chromium, Playwright
`route.fetch` and the context `APIRequestContext` all reach the network only
through it (Playwright's `proxy` launch option; Playwright also forces
`<-loopback>` into Chromium's bypass list so localhost goes through the proxy
too). Windows Firewall rules (install/apply_user_pool_firewall.ps1) are the OS
layer under it, and selftest.py proves both before any claim.

What the proxy enforces, for every connection:
  - CONNECT host:port (https, wss) and absolute-form plain http only; any other
    request shape is refused.
  - Host name rules before any DNS: no localhost, no single-label names, no
    .local/.lan/.internal/.home.arpa/.localdomain/.arpa names, no zone ids.
    IPv4 literals in decimal, octal, hex or short dotted form
    (`167772165`, `0xA000005`, `012.0.0.5`, `10.5`) are parsed as addresses.
  - Ports 1 to 65535 except Chromium's restricted-port list.
  - DNS: resolved here, per connection (no cache), only through fixed public
    resolvers (UDP 53 with TCP fallback on truncation, then DNS over HTTPS to
    an IP literal), so internal names never resolve and the LAN DNS server is
    never asked. Every answer must be public, or the host is refused (a
    rebinding host that mixes public and private answers is refused).
  - Address rule: allowed only when Python `ipaddress` calls it global after
    unwrapping IPv4-mapped, IPv4-compatible, 6to4, Teredo and NAT64
    (64:ff9b::/96), and it is not multicast, loopback, link-local, site-local,
    reserved, unspecified, private or CGNAT 100.64.0.0/10.
  - Office-owned public addresses: the office WAN IPv4 (a page reaching it
    would loop back into the LAN through hairpin NAT) and the site's routed
    IPv6 prefixes are global, so the rule above allows them. The firewall
    script records them in floor.json as `extra_blocked`; the worker passes
    them to each proxy (`--extra-blocked`), which refuses them as "office".
  - The connection goes to the checked IP (pinned), never to the name again.
  - Refusals answer 403 with `X-BDR-Egress: refused` and a reason code; a
    refusal event (host, port, reason; never a path or query) is written to
    stdout as one JSON line for the worker.

Public interface (WI-9 guard and WI-10 tools use these; everything else here is
internal):

  Pure checks (same rules as the proxy; use them before a request exists):
    parse_ip_literal(host) -> IPv4Address | IPv6Address | None
        Raises EgressRefused("ip_zone" | "invalid_host") for zone ids and
        malformed bracketed literals.
    unwrap_address(ip) -> IPv4Address | IPv6Address
    address_refusal(ip) -> str | None          None means public (allowed)
    is_public_address(ip) -> bool
    host_refusal(host) -> str | None           name rules, literals checked as addresses
    target_refusal(host, port) -> str | None   host_refusal plus port rules
    url_refusal(url) -> str | None             scheme (http, https, ws, wss) + target
    REFUSAL_REASONS                            every reason code above
    RESTRICTED_PORTS                           Chromium's restricted-port list

  Per-job process (browser.py starts one per user job; WI-10 never needs to):
    proc = EgressProcess(job_id=..., marker=<job dir>, on_event=callable)
    await proc.start() -> "http://127.0.0.1:<port>"
    proc.playwright_proxy -> {"server": "http://127.0.0.1:<port>"}
    proc.alive -> bool ; proc.pid ; proc.refusals (list of the last events)
    await proc.stop()
  The child exits by itself when its stdin closes (worker gone) and is also
  killed with the worker's Windows Job Object (browser.py).

  In-process server (tests, and the child's own main):
    EgressProxy(resolver=PublicResolver(), connector=None, on_event=None)
    await proxy.start() -> port ; await proxy.close()
  `connector(ip, port)` is a transport seam for tests only: it is called with
  an address that already passed the floor and cannot change the decision.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import ipaddress
import json
import logging
import os
import random
import re
import struct
import sys
import threading
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ...protocol import BLOCKED_HOST_SUFFIXES

log = logging.getLogger("bdr_agent.user_browser_v2.egress")

MODULE = "bdr_agent.agents.user_browser_v2.egress"
IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

# Chromium's kRestrictedPorts (net/base/port_util.cc). Chromium refuses these
# itself; the proxy refuses them for route.fetch and APIRequestContext too.
RESTRICTED_PORTS = frozenset({
    1, 7, 9, 11, 13, 15, 17, 19, 20, 21, 22, 23, 25, 37, 42, 43, 53, 69, 77, 79, 87, 95, 101, 102, 103,
    104, 109, 110, 111, 113, 115, 117, 119, 123, 135, 137, 139, 143, 161, 179, 389, 427, 465, 512, 513,
    514, 515, 526, 530, 531, 532, 540, 548, 554, 556, 563, 587, 601, 636, 989, 990, 993, 995, 1719, 1720,
    1723, 2049, 3659, 4045, 4190, 5060, 5061, 6000, 6566, 6665, 6666, 6667, 6668, 6669, 6679, 6697, 10080,
})

# Names that never leave the PC (plan 10.1 item 4, plus the reverse zones and
# the conventional local suffixes).
INTERNAL_SUFFIXES = tuple(BLOCKED_HOST_SUFFIXES) + (".localhost", ".localdomain", ".arpa", ".intranet")

REFUSAL_REASONS = (
    "bad_request", "scheme", "port", "restricted_port", "invalid_host", "localhost", "single_label",
    "internal_name", "ip_zone", "unspecified", "loopback", "link_local", "site_local", "multicast",
    "private", "cgnat", "reserved", "not_global", "dns_failed", "dns_private", "connect_failed",
    "too_many_connections", "office",
)

_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_IPV4_COMPAT = ipaddress.ip_network("::/96")
_LABEL_RE = re.compile(r"^[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?$")


class EgressRefused(Exception):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


# Address and name rules ----------------------------------------------------------------


def _ipv4_number(part: str) -> int | None:
    """One WHATWG IPv4 part: 0x hex, leading-zero octal, or decimal."""
    if not part:
        return None
    try:
        if part[:2] in ("0x", "0X"):
            return int(part[2:] or "0", 16)
        if len(part) > 1 and part[0] == "0":
            return int(part[1:], 8)
        if part.isdigit():
            return int(part, 10)
    except ValueError:
        return None
    return None


def _parse_ipv4_like(host: str) -> ipaddress.IPv4Address | None:
    """WHATWG URL host IPv4 parser: what Chromium would treat as an address."""
    parts = host.split(".")
    if parts and parts[-1] == "" and len(parts) > 1:
        parts = parts[:-1]
    if not parts or len(parts) > 4:
        return None
    # WHATWG "ends in a number": only then is the host an IPv4 candidate (and
    # a malformed one is an error, never a DNS name).
    if not (parts[-1].isdigit() and parts[-1].isascii()) and not re.fullmatch(r"0[xX][0-9a-fA-F]*", parts[-1]):
        return None
    nums = [_ipv4_number(p) for p in parts]
    if any(n is None for n in nums):
        raise EgressRefused("invalid_host", "malformed IPv4 literal")
    *head, last = nums
    if any(n > 255 for n in head) or last >= 256 ** (5 - len(nums)):  # type: ignore[operator]
        raise EgressRefused("invalid_host", "IPv4 literal out of range")
    value = last
    for i, n in enumerate(head):
        value += n * 256 ** (3 - i)  # type: ignore[operator]
    return ipaddress.IPv4Address(value)


def parse_ip_literal(host: str) -> IPAddress | None:
    """The address a host string denotes, or None for a DNS name."""
    h = (host or "").strip().lower()
    if h.startswith("[") or h.endswith("]"):
        if not (h.startswith("[") and h.endswith("]")):
            raise EgressRefused("invalid_host", "unbalanced brackets")
        h = h[1:-1]
        if "%" in h:
            raise EgressRefused("ip_zone", "IPv6 zone id")
        try:
            return ipaddress.IPv6Address(h)
        except ValueError:
            raise EgressRefused("invalid_host", "bad IPv6 literal") from None
    if ":" in h:
        if "%" in h:
            raise EgressRefused("ip_zone", "IPv6 zone id")
        try:
            return ipaddress.IPv6Address(h)
        except ValueError:
            raise EgressRefused("invalid_host", "bad IPv6 literal") from None
    return _parse_ipv4_like(h)


def unwrap_address(ip: IPAddress) -> IPAddress:
    """The IPv4 address an IPv6 form carries, when it carries one."""
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return ip.ipv4_mapped
        if ip.sixtofour is not None:
            return ip.sixtofour
        if ip.teredo is not None:
            return ip.teredo[1]
        if ip in _NAT64:
            return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if ip in _IPV4_COMPAT and int(ip) > 1:
            return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return ip


IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


def parse_networks(values: Iterable[Any] | None) -> tuple[IPNetwork, ...]:
    """CIDR strings (or networks) to networks; anything unparsable is
    dropped. Used for floor.json `extra_blocked` (office-owned addresses)."""
    out: list[IPNetwork] = []
    for v in values or ():
        if isinstance(v, (ipaddress.IPv4Network, ipaddress.IPv6Network)):
            out.append(v)
            continue
        if not isinstance(v, str) or not v.strip():
            continue
        try:
            out.append(ipaddress.ip_network(v.strip(), strict=False))
        except ValueError:
            log.warning("ignored an extra blocked range that is not a network: %r", v[:60])
    return tuple(dict.fromkeys(out))


def address_refusal(ip: IPAddress | str, extra_blocked: Iterable[IPNetwork] = ()) -> str | None:
    """None when `ip` may be contacted; otherwise the reason code.
    `extra_blocked`: office-owned public networks (floor.json), refused as
    "office" in their plain and every embedded IPv4 form."""
    if isinstance(ip, str):
        try:
            ip = ipaddress.ip_address(ip)
        except ValueError:
            return "invalid_host"
    u = unwrap_address(ip)
    for net in extra_blocked:
        if any(c.version == net.version and c in net for c in (ip, u)):
            return "office"
    for candidate in ((ip, u) if u is not ip else (ip,)):
        if candidate.is_unspecified:
            return "unspecified"
        if candidate.is_loopback:
            return "loopback"
        if candidate.is_link_local:
            return "link_local"
        if isinstance(candidate, ipaddress.IPv6Address) and candidate.is_site_local:
            return "site_local"
        if candidate.is_multicast:
            return "multicast"
    if isinstance(u, ipaddress.IPv4Address) and u in _CGNAT:
        return "cgnat"
    if u.is_private:
        return "private"
    if u.is_reserved:
        return "reserved"
    if not u.is_global:
        return "not_global"
    return None


def is_public_address(ip: IPAddress | str) -> bool:
    return address_refusal(ip) is None


def host_refusal(host: str) -> str | None:
    """Pre-DNS rules for a host name or literal. None means "may be resolved"."""
    if not isinstance(host, str) or not host.strip():
        return "invalid_host"
    try:
        literal = parse_ip_literal(host)
    except EgressRefused as exc:
        return exc.reason
    if literal is not None:
        return address_refusal(literal)
    h = host.strip().lower()
    h = h.removesuffix(".")
    if not h or len(h) > 253 or not h.isascii():
        return "invalid_host"
    if h == "localhost" or h.endswith(".localhost"):
        return "localhost"
    if "." not in h:
        return "single_label"
    if any(h.endswith(s) for s in INTERNAL_SUFFIXES):
        return "internal_name"
    if not all(_LABEL_RE.match(label) for label in h.split(".")):
        return "invalid_host"
    return None


def target_refusal(host: str, port: int | None) -> str | None:
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        return "port"
    if port in RESTRICTED_PORTS:
        return "restricted_port"
    return host_refusal(host)


def url_refusal(url: str) -> str | None:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return "invalid_host"
    scheme = (parts.scheme or "").lower()
    defaults = {"http": 80, "https": 443, "ws": 80, "wss": 443}
    if scheme not in defaults:
        return "scheme"
    netloc_host = parts.netloc.rpartition("@")[2]
    if netloc_host.startswith("["):
        host = netloc_host[: netloc_host.find("]") + 1]
    else:
        host = netloc_host.rsplit(":", 1)[0] if netloc_host.count(":") == 1 else netloc_host
    return target_refusal(host, port if port is not None else defaults[scheme])


# DNS through fixed public resolvers -----------------------------------------------------

QTYPE_A = 1
QTYPE_AAAA = 28
_QTYPE_CNAME = 5
DEFAULT_UDP_SERVERS = ("1.1.1.1", "8.8.8.8", "9.9.9.9")
DEFAULT_DOH_URLS = ("https://1.1.1.1/dns-query", "https://8.8.8.8/dns-query")


class DnsError(Exception):
    pass


def encode_dns_name(name: str) -> bytes:
    out = b""
    for label in name.rstrip(".").split("."):
        raw = label.encode("ascii")
        if not 1 <= len(raw) <= 63:
            raise DnsError("bad label")
        out += bytes([len(raw)]) + raw
    return out + b"\x00"


def build_dns_query(name: str, qtype: int, qid: int) -> bytes:
    header = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)   # RD set
    return header + encode_dns_name(name) + struct.pack(">HH", qtype, 1)


def _read_name(data: bytes, offset: int) -> tuple[str, int]:
    labels: list[str] = []
    jumps = 0
    end: int | None = None
    while True:
        if offset >= len(data):
            raise DnsError("truncated name")
        length = data[offset]
        if length & 0xC0 == 0xC0:
            if offset + 1 >= len(data):
                raise DnsError("truncated pointer")
            if end is None:
                end = offset + 2
            offset = ((length & 0x3F) << 8) | data[offset + 1]
            jumps += 1
            if jumps > 32:
                raise DnsError("pointer loop")
            continue
        if length == 0:
            offset += 1
            break
        labels.append(data[offset + 1: offset + 1 + length].decode("ascii", "replace"))
        offset += 1 + length
    return ".".join(labels).lower(), (end if end is not None else offset)


def parse_dns_response(data: bytes, qid: int, name: str, qtype: int) -> tuple[int, list[str], bool]:
    """(rcode, addresses of qtype, truncated). Raises DnsError on a reply that
    is not the answer to this exact question."""
    if len(data) < 12:
        raise DnsError("short reply")
    rid, flags, qd, an, _ns, _ar = struct.unpack(">HHHHHH", data[:12])
    if rid != qid or not flags & 0x8000:
        raise DnsError("reply id or flags mismatch")
    truncated = bool(flags & 0x0200)
    rcode = flags & 0x000F
    offset = 12
    if qd != 1:
        raise DnsError("unexpected question count")
    qname, offset = _read_name(data, offset)
    if offset + 4 > len(data):
        raise DnsError("truncated question")
    rq_type, _rq_class = struct.unpack(">HH", data[offset: offset + 4])
    offset += 4
    if qname != name.rstrip(".").lower() or rq_type != qtype:
        raise DnsError("reply is for another question")
    addrs: list[str] = []
    for _ in range(an):
        _rname, offset = _read_name(data, offset)
        if offset + 10 > len(data):
            raise DnsError("truncated answer")
        rtype, _rclass, _ttl, rdlen = struct.unpack(">HHIH", data[offset: offset + 10])
        offset += 10
        rdata = data[offset: offset + rdlen]
        if len(rdata) != rdlen:
            raise DnsError("truncated rdata")
        offset += rdlen
        if rtype == qtype == QTYPE_A and rdlen == 4:
            addrs.append(str(ipaddress.IPv4Address(rdata)))
        elif rtype == qtype == QTYPE_AAAA and rdlen == 16:
            addrs.append(str(ipaddress.IPv6Address(rdata)))
    return rcode, addrs, truncated


class _UdpQuery(asyncio.DatagramProtocol):
    def __init__(self, future: asyncio.Future):
        self.future = future

    def datagram_received(self, data: bytes, addr: Any) -> None:
        if not self.future.done():
            self.future.set_result(data)

    def error_received(self, exc: Exception) -> None:
        if not self.future.done():
            self.future.set_exception(exc)


class PublicResolver:
    """A and AAAA lookups through fixed public resolvers only. Never the
    system resolver, never the LAN DNS server."""

    def __init__(self, *, udp_servers: Iterable[str] = DEFAULT_UDP_SERVERS,
                 doh_urls: Iterable[str] = DEFAULT_DOH_URLS, timeout: float = 2.5):
        self.udp_servers = tuple(udp_servers)
        self.doh_urls = tuple(doh_urls)
        self.timeout = timeout
        for server in self.udp_servers:
            if address_refusal(server) is not None:
                raise ValueError(f"resolver {server} is not a public address")
        for url in self.doh_urls:
            host = urlsplit(url).hostname or ""
            if urlsplit(url).scheme != "https" or address_refusal(host) is not None:
                raise ValueError("DoH resolvers must be https:// public IP literals")

    async def _udp(self, server: str, query: bytes) -> bytes:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        transport, _ = await loop.create_datagram_endpoint(lambda: _UdpQuery(fut), remote_addr=(server, 53))
        try:
            transport.sendto(query)
            return await asyncio.wait_for(fut, self.timeout)
        finally:
            transport.close()

    async def _tcp(self, server: str, query: bytes) -> bytes:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(server, 53), self.timeout)
        try:
            writer.write(struct.pack(">H", len(query)) + query)
            await writer.drain()
            (length,) = struct.unpack(">H", await asyncio.wait_for(reader.readexactly(2), self.timeout))
            return await asyncio.wait_for(reader.readexactly(length), self.timeout)
        finally:
            writer.close()

    async def _doh(self, url: str, query: bytes) -> bytes:
        import httpx

        async with httpx.AsyncClient(timeout=self.timeout, trust_env=False, follow_redirects=False) as client:
            resp = await client.post(url, content=query, headers={
                "content-type": "application/dns-message", "accept": "application/dns-message"})
            if resp.status_code != 200:
                raise DnsError(f"DoH status {resp.status_code}")
            return resp.content

    async def lookup(self, name: str, qtype: int) -> list[str]:
        """Addresses of one type. [] for NXDOMAIN or no records. Raises
        DnsError when no resolver answered."""
        last: Exception | None = None
        for server in self.udp_servers:
            qid = random.SystemRandom().randrange(1, 65536)
            query = build_dns_query(name, qtype, qid)
            try:
                rcode, addrs, truncated = parse_dns_response(await self._udp(server, query), qid, name, qtype)
                if truncated:
                    rcode, addrs, _ = parse_dns_response(await self._tcp(server, query), qid, name, qtype)
            except (OSError, TimeoutError, DnsError, asyncio.IncompleteReadError) as exc:
                last = exc
                continue
            if rcode in (0, 3):
                return addrs
            last = DnsError(f"rcode {rcode}")
        for url in self.doh_urls:
            qid = random.SystemRandom().randrange(1, 65536)
            query = build_dns_query(name, qtype, qid)
            try:
                rcode, addrs, _ = parse_dns_response(await self._doh(url, query), qid, name, qtype)
            except Exception as exc:   # httpx errors, timeouts, bad replies
                last = exc
                continue
            if rcode in (0, 3):
                return addrs
            last = DnsError(f"rcode {rcode}")
        raise DnsError(f"no resolver answered: {last}")

    async def resolve(self, host: str) -> list[str]:
        results = await asyncio.gather(self.lookup(host, QTYPE_A), self.lookup(host, QTYPE_AAAA),
                                       return_exceptions=True)
        addrs: list[str] = []
        errors = 0
        for r in results:
            if isinstance(r, BaseException):
                errors += 1
            else:
                addrs.extend(r)
        if errors == len(results):
            raise DnsError("lookup failed")
        return addrs


Resolver = Callable[[str], Awaitable[list[str]]]
Connector = Callable[[str, int], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]


async def checked_addresses(host: str, resolve: Resolver,
                            extra_blocked: Iterable[IPNetwork] = ()) -> list[str]:
    """Every address `host` may be contacted at, IPv4 first. Raises
    EgressRefused. A literal is checked as is; a name is resolved and refused
    if ANY answer is not public (or is office-owned, `extra_blocked`)."""
    extra = tuple(extra_blocked)
    reason = host_refusal(host)
    if reason is not None:
        raise EgressRefused(reason, host)
    literal = parse_ip_literal(host)
    if literal is not None:
        reason = address_refusal(literal, extra)
        if reason is not None:
            raise EgressRefused(reason, host)
        return [str(literal)]
    try:
        addrs = await resolve(host.strip().lower().rstrip("."))
    except Exception as exc:
        raise EgressRefused("dns_failed", type(exc).__name__) from None
    if not addrs:
        raise EgressRefused("dns_failed", "no address")
    for a in addrs:
        reason = address_refusal(a, extra)
        if reason is not None:
            raise EgressRefused("office" if reason == "office" else "dns_private", host)
    return sorted(set(addrs), key=lambda a: (":" in a, addrs.index(a)))


# The proxy -------------------------------------------------------------------------------

_HOP_HEADERS = {"proxy-connection", "proxy-authorization", "proxy-authenticate", "connection", "keep-alive",
                "te", "trailer", "upgrade"}
HEAD_LIMIT = 64 * 1024
IDLE_SECONDS = 300.0
CONNECT_SECONDS = 15.0
MAX_CONNECTIONS = 256


async def _default_connector(ip: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await asyncio.open_connection(ip, port)


def _refusal_response(status: int, reason: str) -> bytes:
    body = f"Blocked by the BDR egress floor: {reason}\n".encode()
    text = {403: "Forbidden", 400: "Bad Request", 502: "Bad Gateway", 503: "Service Unavailable"}[status]
    return (f"HTTP/1.1 {status} {text}\r\nContent-Type: text/plain\r\nX-BDR-Egress: refused; reason={reason}\r\n"
            f"Connection: close\r\nContent-Length: {len(body)}\r\n\r\n").encode() + body


async def _graceful_close(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Flush what we wrote, half-close, give the client a moment to read it
    and close its side, then close. A bare close with unread request bytes
    makes the OS reset the connection before the client reads a refusal."""
    async def drain_client() -> None:
        while await reader.read(65536):
            pass

    with contextlib.suppress(Exception):
        await asyncio.wait_for(writer.drain(), 5)
        if writer.can_write_eof():
            writer.write_eof()
        await asyncio.wait_for(drain_client(), 3)
    with contextlib.suppress(Exception):
        writer.close()


def _split_target(target: str) -> tuple[str, int | None]:
    if target.startswith("["):
        end = target.find("]")
        if end < 0:
            raise EgressRefused("invalid_host", "bad CONNECT target")
        host, rest = target[: end + 1], target[end + 1:]
        port_s = rest[1:] if rest.startswith(":") else ""
    else:
        host, _, port_s = target.rpartition(":")
    if not host or not port_s.isdigit():
        raise EgressRefused("port", "CONNECT needs host:port")
    return host, int(port_s)


class EgressProxy:
    def __init__(self, *, resolver: PublicResolver | Resolver | None = None, connector: Connector | None = None,
                 on_event: Callable[[dict[str, Any]], None] | None = None,
                 idle_seconds: float = IDLE_SECONDS, max_connections: int = MAX_CONNECTIONS,
                 extra_blocked: Iterable[Any] = ()):
        self.extra_blocked = parse_networks(extra_blocked)
        r = resolver if resolver is not None else PublicResolver()
        self._resolve: Resolver = r.resolve if isinstance(r, PublicResolver) else r
        self._connect = connector or _default_connector
        self._on_event = on_event
        self.idle_seconds = idle_seconds
        self._slots = asyncio.Semaphore(max_connections)
        self._server: asyncio.AbstractServer | None = None
        self._tasks: set[asyncio.Task] = set()
        self.port: int | None = None
        self.stats = {"connections": 0, "refused": 0, "bytes_up": 0, "bytes_down": 0}

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> int:
        if host not in ("127.0.0.1", "::1"):
            raise ValueError("the egress proxy listens on loopback only")
        self._server = await asyncio.start_server(self._handle, host, port, limit=HEAD_LIMIT)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(BaseException):
                await task

    def _event(self, kind: str, host: str, port: int | None, reason: str) -> None:
        self.stats["refused"] += 1
        if self._on_event is not None:
            with contextlib.suppress(Exception):
                self._on_event({"event": "refused", "kind": kind, "host": host[:253], "port": port,
                                "reason": reason})

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        try:
            if self._slots.locked():
                # Read the request first: closing with unread data would reset
                # the connection before the client sees the answer.
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
                writer.write(_refusal_response(503, "too_many_connections"))
                await writer.drain()
                return
            async with self._slots:
                self.stats["connections"] += 1
                await self._serve(reader, writer)
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
            pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:   # never let one connection kill the proxy
            log.debug("egress connection error: %s", type(exc).__name__)
        finally:
            await _graceful_close(reader, writer)
            if task is not None:
                self._tasks.discard(task)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 30)
        try:
            text = head.decode("latin-1")
            request_line, *header_lines = text[:-4].split("\r\n")
            method, target, version = request_line.split(" ")
        except ValueError:
            writer.write(_refusal_response(400, "bad_request"))
            return
        if not version.startswith("HTTP/1."):
            writer.write(_refusal_response(400, "bad_request"))
            return
        if method.upper() == "CONNECT":
            await self._connect_tunnel(target, reader, writer)
        else:
            await self._forward_http(method, target, version, header_lines, reader, writer)

    async def _open_checked(self, kind: str, host: str, port: int,
                            writer: asyncio.StreamWriter) -> tuple[asyncio.StreamReader, asyncio.StreamWriter] | None:
        reason = target_refusal(host, port)
        if reason is not None:
            self._event(kind, host, port, reason)
            writer.write(_refusal_response(403, reason))
            return None
        try:
            addrs = await checked_addresses(host, self._resolve, self.extra_blocked)
        except EgressRefused as exc:
            self._event(kind, host, port, exc.reason)
            writer.write(_refusal_response(403 if exc.reason != "dns_failed" else 502, exc.reason))
            return None
        for ip in addrs:
            # Defense in depth: the pinned address is checked again right
            # before the socket opens.
            if address_refusal(ip, self.extra_blocked) is not None:
                continue
            try:
                return await asyncio.wait_for(self._connect(ip, port), CONNECT_SECONDS)
            except (OSError, TimeoutError):
                continue
        writer.write(_refusal_response(502, "connect_failed"))
        return None

    async def _connect_tunnel(self, target: str, reader: asyncio.StreamReader,
                              writer: asyncio.StreamWriter) -> None:
        try:
            host, port = _split_target(target)
        except EgressRefused as exc:
            self._event("connect", target[:100], None, exc.reason)
            writer.write(_refusal_response(400, exc.reason))
            return
        upstream = await self._open_checked("connect", host, port, writer)
        if upstream is None:
            return
        up_reader, up_writer = upstream
        try:
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            await self._pipe(reader, writer, up_reader, up_writer)
        finally:
            with contextlib.suppress(Exception):
                up_writer.close()

    async def _forward_http(self, method: str, target: str, version: str, header_lines: list[str],
                            reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            parts = urlsplit(target)
            port = parts.port
        except ValueError:
            writer.write(_refusal_response(400, "bad_request"))
            return
        if parts.scheme.lower() != "http" or not parts.hostname:
            self._event("http", parts.hostname or "", None, "scheme")
            writer.write(_refusal_response(400, "scheme"))
            return
        host_text = parts.netloc.rpartition("@")[2]
        host = host_text[: host_text.find("]") + 1] if host_text.startswith("[") else host_text.rsplit(":", 1)[0]
        port = port or 80
        upstream = await self._open_checked("http", host, port, writer)
        if upstream is None:
            return
        up_reader, up_writer = upstream
        drop = set(_HOP_HEADERS)
        headers: list[str] = []
        for line in header_lines:
            name, _, value = line.partition(":")
            if name.strip().lower() == "connection":
                drop.update(v.strip().lower() for v in value.split(","))
        # The Host header always names the checked target (RFC 9112 3.2.2).
        drop.add("host")
        headers = [f"Host: {host_text}"]
        for line in header_lines:
            name = line.partition(":")[0].strip().lower()
            if name and name not in drop:
                headers.append(line)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        head = f"{method} {path} {version}\r\n" + "".join(h + "\r\n" for h in headers) + "Connection: close\r\n\r\n"
        try:
            up_writer.write(head.encode("latin-1"))
            await up_writer.drain()
            await self._pipe(reader, writer, up_reader, up_writer)
        finally:
            with contextlib.suppress(Exception):
                up_writer.close()

    async def _pipe(self, c_reader: asyncio.StreamReader, c_writer: asyncio.StreamWriter,
                    u_reader: asyncio.StreamReader, u_writer: asyncio.StreamWriter) -> None:
        async def copy(src: asyncio.StreamReader, dst: asyncio.StreamWriter, counter: str) -> None:
            try:
                while True:
                    chunk = await asyncio.wait_for(src.read(256 * 1024), self.idle_seconds)
                    if not chunk:
                        break
                    self.stats[counter] += len(chunk)
                    dst.write(chunk)
                    await dst.drain()
            except (ConnectionError, OSError, TimeoutError):
                pass
            finally:
                with contextlib.suppress(Exception):
                    if dst.can_write_eof():
                        dst.write_eof()

        up = asyncio.create_task(copy(c_reader, u_writer, "bytes_up"))
        down = asyncio.create_task(copy(u_reader, c_writer, "bytes_down"))
        try:
            done, _ = await asyncio.wait({up, down}, return_when=asyncio.FIRST_COMPLETED)
            if down not in done:
                # The client finished sending (or left); let the answer finish.
                await asyncio.wait({down}, timeout=30)
        finally:
            for t in (up, down):
                t.cancel()
            with contextlib.suppress(Exception):
                u_writer.close()


# Child process ----------------------------------------------------------------------------


def _watch_stdin(loop: asyncio.AbstractEventLoop, stop: asyncio.Event) -> None:
    """The worker holds our stdin open; EOF means it is gone."""
    try:
        while sys.stdin.buffer.read(4096):
            pass
    except Exception:
        pass
    loop.call_soon_threadsafe(stop.set)


async def _child_main(args: argparse.Namespace) -> int:
    lock = threading.Lock()

    def emit(obj: dict[str, Any]) -> None:
        line = json.dumps(obj, separators=(",", ":")) + "\n"
        with lock:
            sys.stdout.write(line)
            sys.stdout.flush()

    proxy = EgressProxy(on_event=emit, extra_blocked=args.extra_blocked or ())
    port = await proxy.start()
    emit({"event": "ready", "port": port, "pid": os.getpid()})
    stop = asyncio.Event()
    threading.Thread(target=_watch_stdin, args=(asyncio.get_running_loop(), stop), daemon=True).start()
    await stop.wait()
    await proxy.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog=MODULE)
    p.add_argument("--job", required=True)
    # The job directory. Only there so boot cleanup can find (and kill) this
    # process by its command line after a worker crash.
    p.add_argument("--marker", required=True)
    # Office-owned public networks from floor.json (repeatable).
    p.add_argument("--extra-blocked", action="append", default=[])
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    try:
        return asyncio.run(_child_main(args))
    except KeyboardInterrupt:
        return 0


# Parent side ---------------------------------------------------------------------------------


class EgressStartError(RuntimeError):
    pass


class EgressProcess:
    """One egress proxy child per user job."""

    def __init__(self, *, job_id: str, marker: Path | str, python: str | None = None,
                 on_event: Callable[[dict[str, Any]], None] | None = None, startup_timeout: float = 20.0,
                 extra_blocked: Iterable[Any] = ()):
        self.job_id = str(job_id)
        self.extra_blocked = parse_networks(extra_blocked)
        self.marker = str(marker)
        self.python = python or sys.executable
        self._on_event = on_event
        self.startup_timeout = startup_timeout
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task | None = None
        self._err_task: asyncio.Task | None = None
        self.returncode: int | None = None
        self._stderr: deque[str] = deque(maxlen=40)
        self.port: int | None = None
        self.refusals: deque[dict[str, Any]] = deque(maxlen=200)

    @property
    def server(self) -> str:
        if self.port is None:
            raise EgressStartError("egress proxy not started")
        return f"http://127.0.0.1:{self.port}"

    @property
    def playwright_proxy(self) -> dict[str, str]:
        return {"server": self.server}

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    def command(self) -> list[str]:
        cmd = [self.python, "-I", "-m", MODULE, "--job", self.job_id, "--marker", self.marker]
        for net in self.extra_blocked:
            cmd += ["--extra-blocked", str(net)]
        return cmd

    async def start(self) -> str:
        env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
        self._proc = await asyncio.create_subprocess_exec(
            *self.command(), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=env)
        ready: asyncio.Future = asyncio.get_running_loop().create_future()
        self._reader = asyncio.create_task(self._read(ready), name=f"egress-{self.job_id}")
        self._err_task = asyncio.create_task(self._drain_stderr(), name=f"egress-err-{self.job_id}")
        try:
            self.port = await asyncio.wait_for(ready, self.startup_timeout)
        except (TimeoutError, EgressStartError) as exc:
            await self.stop()
            raise EgressStartError(f"egress proxy did not start: {exc}; {' | '.join(self._stderr)[-500:]}") \
                from None
        return self.server

    async def _read(self, ready: asyncio.Future) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        try:
            async for raw in self._proc.stdout:
                try:
                    obj = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(obj, dict):
                    continue
                if obj.get("event") == "ready" and not ready.done():
                    port = obj.get("port")
                    if isinstance(port, int) and 0 < port < 65536:
                        ready.set_result(port)
                    continue
                if obj.get("event") == "refused":
                    self.refusals.append(obj)
                    if self._on_event is not None:
                        with contextlib.suppress(Exception):
                            self._on_event(obj)
        finally:
            if not ready.done():
                ready.set_exception(EgressStartError("egress proxy exited"))
            elif self.port is not None:
                log.warning("egress proxy for job %s exited", self.job_id)

    async def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        with contextlib.suppress(Exception):
            async for raw in proc.stderr:
                self._stderr.append(raw.decode("utf-8", "replace").rstrip()[:300])

    async def stop(self, timeout: float = 5.0) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        if proc.returncode is None:
            with contextlib.suppress(Exception):
                if proc.stdin is not None:
                    proc.stdin.close()
            try:
                await asyncio.wait_for(proc.wait(), timeout)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(proc.wait(), timeout)
        for task in (self._reader, self._err_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
        self._reader = self._err_task = None
        self.returncode = proc.returncode


if __name__ == "__main__":
    sys.exit(main())
