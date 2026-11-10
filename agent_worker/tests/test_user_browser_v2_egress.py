"""WI-8: the egress floor (plan 10.1 layer 1, 10.3) and the per-job process
(10.6, 12.3, 15.4).

Pure checks, the DNS wire code and the in-process proxy run everywhere. The
proxy tests use a fake resolver and a transport seam that maps one public
address to a local server, so nothing leaves the machine; the seam is only
ever called with an address that already passed the floor (asserted). The
real-Chromium tests (needs_browser) run the real egress child process and a
real per-job Chromium and check that loopback and private targets are
unreachable from the page, from route.fetch and from the APIRequestContext.
"""

import asyncio
import contextlib
import ipaddress
import json
import os
import re
import socket
import struct
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest

from bdr_agent.agents.user_browser_v2 import egress as E
from bdr_agent.agents.user_browser_v2.selftest import EgressFloorUnavailable, UserPoolLayout
from bdr_agent.browser import browser as B
from bdr_agent.heartbeat import JobTracker
from bdr_agent.protocol import ClaimedJob, TypeConfig
from bdr_agent.redact import LiveSecrets
from bdr_agent.slot import Finish, SetupUnavailable, Slot, SlotDeps, UserSlotParts
from bdr_agent.spool import Spool

from .agent_harness import needs_browser

PUBLIC_IP = "93.184.216.34"


# Pure checks -------------------------------------------------------------------------------

REFUSED_HOSTS = {
    "192.168.1.1": "private",
    "10.0.0.5": "private",
    "167772165": "private",            # decimal 10.0.0.5
    "0xA000005": "private",            # hex 10.0.0.5
    "0xa.0.0.5": "private",
    "012.0.0.5": "private",            # octal 10.0.0.5
    "10.5": "private",                 # short dotted 10.0.0.5
    "127.0.0.1": "loopback",
    "2130706433": "loopback",
    "0.0.0.0": "unspecified",
    "0": "unspecified",
    "169.254.169.254": "link_local",
    "100.64.0.1": "cgnat",
    "224.0.0.251": "multicast",
    "255.255.255.255": "private",
    "[fd00::1]": "private",            # ULA
    "fd12:3456::1": "private",
    "[::1]": "loopback",
    "[::]": "unspecified",
    "[fe80::1]": "link_local",
    "[fe80::1%25eth0]": "ip_zone",
    "[fec0::1]": "site_local",
    "[ff02::1]": "multicast",
    "::ffff:10.0.0.5": "private",      # IPv4-mapped
    "[::ffff:127.0.0.1]": "loopback",
    "[::a00:5]": "private",            # IPv4-compatible
    "64:ff9b::a00:5": "private",       # NAT64
    "[2002:a00:5::1]": "private",      # 6to4 of 10.0.0.5
    "[2001:0:4136:e378:8000:63bf:3fff:fdd2]": "private",   # Teredo (client in a doc range)
    "localhost": "localhost",
    "LOCALHOST.": "localhost",
    "app.localhost": "localhost",
    "router": "single_label",
    "router.local": "internal_name",
    "nas.lan": "internal_name",
    "db.internal": "internal_name",
    "printer.home.arpa": "internal_name",
    "5.0.0.10.in-addr.arpa": "internal_name",
    "box.localdomain": "internal_name",
    "1.2.3.08": "invalid_host",
    "256.1.1.1": "invalid_host",
    "exa mple.com": "invalid_host",
    "": "invalid_host",
}


@pytest.mark.parametrize(("host", "reason"), sorted(REFUSED_HOSTS.items()))
def test_host_floor_refuses_private_and_internal_targets(host, reason):
    assert E.host_refusal(host) == reason


@pytest.mark.parametrize("host", ["example.com", "app.procore.com.", "8.8.8.8", "[2606:4700::1111]",
                                  "2606:4700::1111", "::ffff:8.8.8.8", "64:ff9b::808:808", "a_b.example.com"])
def test_host_floor_allows_public_targets(host):
    assert E.host_refusal(host) is None


def test_address_rule_unwraps_every_embedded_ipv4_form():
    assert E.unwrap_address(ipaddress.ip_address("::ffff:10.0.0.5")) == ipaddress.ip_address("10.0.0.5")
    assert E.unwrap_address(ipaddress.ip_address("64:ff9b::a00:5")) == ipaddress.ip_address("10.0.0.5")
    assert E.unwrap_address(ipaddress.ip_address("2002:a00:5::1")) == ipaddress.ip_address("10.0.0.5")
    assert E.unwrap_address(ipaddress.ip_address("::a00:5")) == ipaddress.ip_address("10.0.0.5")
    assert E.is_public_address("8.8.8.8") and not E.is_public_address("10.0.0.5")
    assert E.address_refusal("not an ip") == "invalid_host"
    # Python calls these global; the floor does not.
    for text in ("224.0.0.1", "fec0::1", "::a00:5", "64:ff9b::a00:5", "ff02::1"):
        assert E.address_refusal(text) is not None, text


@pytest.mark.parametrize(("url", "reason"), [
    ("http://167772165/", "private"), ("http://0xA000005/", "private"), ("http://router.local/", "internal_name"),
    ("http://192.168.1.1/", "private"), ("https://[fd00::1]/", "private"), ("https://example.com:25/", "restricted_port"),
    ("https://example.com:0/", "port"), ("file:///C:/Windows/win.ini", "scheme"), ("chrome://settings", "scheme"),
    ("javascript:alert(1)", "scheme"), ("https://user:pw@[::1]:443/", "loopback"), ("ftp://example.com/", "scheme"),
])
def test_url_refusal(url, reason):
    assert E.url_refusal(url) == reason


@pytest.mark.parametrize("url", ["https://example.com/x?y=1", "http://example.com:8080/", "wss://example.com/s",
                                 "https://[2606:4700::1111]/"])
def test_url_refusal_allows_public_urls(url):
    assert E.url_refusal(url) is None


def test_restricted_ports_and_reasons_are_stable():
    assert {25, 53, 6667, 10080} <= E.RESTRICTED_PORTS and 443 not in E.RESTRICTED_PORTS
    assert E.target_refusal("example.com", 70000) == "port"
    assert E.target_refusal("example.com", True) == "port"
    for host, reason in REFUSED_HOSTS.items():
        assert reason in E.REFUSAL_REASONS, host


# DNS wire format ------------------------------------------------------------------------------


def _answer(qid, name, qtype, records, *, flags=0x8180, question=None):
    q = E.encode_dns_name(question or name) + struct.pack(">HH", qtype, 1)
    out = struct.pack(">HHHHHH", qid, flags, 1, len(records), 0, 0) + q
    for rtype, rdata in records:
        out += b"\xc0\x0c" + struct.pack(">HHIH", rtype, 1, 60, len(rdata)) + rdata
    return out


def test_dns_query_and_reply_round_trip_with_cname_and_compression():
    q = E.build_dns_query("www.Example.com", E.QTYPE_A, 0x1234)
    assert q[:2] == b"\x12\x34" and q.endswith(struct.pack(">HH", 1, 1))
    reply = _answer(0x1234, "www.example.com", E.QTYPE_A, [
        (5, E.encode_dns_name("edge.example.net")), (1, bytes([93, 184, 216, 34])), (28, bytes(16))])
    rcode, addrs, truncated = E.parse_dns_response(reply, 0x1234, "www.example.com", E.QTYPE_A)
    assert (rcode, addrs, truncated) == (0, [PUBLIC_IP], False)
    v6 = _answer(7, "example.com", E.QTYPE_AAAA, [(28, ipaddress.ip_address("2606:4700::1").packed)])
    assert E.parse_dns_response(v6, 7, "example.com", E.QTYPE_AAAA)[1] == ["2606:4700::1"]


def test_dns_reply_for_another_question_or_id_is_refused():
    reply = _answer(1, "example.com", E.QTYPE_A, [(1, bytes(4))])
    with pytest.raises(E.DnsError):
        E.parse_dns_response(reply, 2, "example.com", E.QTYPE_A)
    with pytest.raises(E.DnsError):
        E.parse_dns_response(reply, 1, "evil.example", E.QTYPE_A)
    with pytest.raises(E.DnsError):
        E.parse_dns_response(reply[:20], 1, "example.com", E.QTYPE_A)
    loop = struct.pack(">HHHHHH", 1, 0x8180, 1, 0, 0, 0) + b"\xc0\x0c"
    with pytest.raises(E.DnsError):
        E.parse_dns_response(loop, 1, "example.com", E.QTYPE_A)
    truncated = _answer(3, "example.com", E.QTYPE_A, [], flags=0x8380)
    assert E.parse_dns_response(truncated, 3, "example.com", E.QTYPE_A)[2] is True


def test_resolver_accepts_only_public_resolvers():
    with pytest.raises(ValueError):
        E.PublicResolver(udp_servers=("192.168.1.1",))
    with pytest.raises(ValueError):
        E.PublicResolver(udp_servers=(), doh_urls=("https://dns.example/dns-query",))
    with pytest.raises(ValueError):
        E.PublicResolver(udp_servers=(), doh_urls=("http://1.1.1.1/dns-query",))


async def test_resolver_falls_back_udp_to_tcp_to_doh(monkeypatch):
    r = E.PublicResolver(udp_servers=("1.1.1.1", "8.8.8.8"), doh_urls=("https://1.1.1.1/dns-query",))
    seen = []

    def reply_for(query, records, flags=0x8180):
        qid = struct.unpack(">H", query[:2])[0]
        return _answer(qid, "example.com", E.QTYPE_A, records, flags=flags)

    async def udp(server, query):
        seen.append(("udp", server))
        if server == "1.1.1.1":
            raise OSError("udp blocked")
        return reply_for(query, [], flags=0x8380)          # truncated

    async def tcp(server, query):
        seen.append(("tcp", server))
        return reply_for(query, [(1, bytes([93, 184, 216, 34]))])

    monkeypatch.setattr(r, "_udp", udp)
    monkeypatch.setattr(r, "_tcp", tcp)
    assert await r.lookup("example.com", E.QTYPE_A) == [PUBLIC_IP]
    assert seen == [("udp", "1.1.1.1"), ("udp", "8.8.8.8"), ("tcp", "8.8.8.8")]

    async def udp_down(server, query):
        raise OSError("down")

    async def doh(url, query):
        return reply_for(query, [(1, bytes([93, 184, 216, 34]))])

    monkeypatch.setattr(r, "_udp", udp_down)
    monkeypatch.setattr(r, "_doh", doh)
    assert await r.lookup("example.com", E.QTYPE_A) == [PUBLIC_IP]

    async def doh_down(url, query):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(r, "_doh", doh_down)
    with pytest.raises(E.DnsError):
        await r.lookup("example.com", E.QTYPE_A)
    with pytest.raises(E.DnsError):
        await r.resolve("example.com")


async def test_checked_addresses_refuse_mixed_and_private_answers():
    async def resolve(host):
        return {"ok.example": [PUBLIC_IP, "2606:4700::1"], "mixed.example": [PUBLIC_IP, "10.0.0.5"],
                "v6.example": ["fd00::1"], "none.example": []}[host]

    assert await E.checked_addresses("ok.example", resolve) == [PUBLIC_IP, "2606:4700::1"]
    for host, reason in (("mixed.example", "dns_private"), ("v6.example", "dns_private"),
                         ("none.example", "dns_failed"), ("router.local", "internal_name")):
        with pytest.raises(E.EgressRefused) as info:
            await E.checked_addresses(host, resolve)
        assert info.value.reason == reason
    assert await E.checked_addresses("8.8.8.8", resolve) == ["8.8.8.8"]


# In-process proxy ------------------------------------------------------------------------------


class Upstream:
    """A local server standing in for a public host: echoes what it got."""

    def __init__(self):
        self.received: list[bytes] = []
        self.server = None
        self.port = None

    async def start(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def _handle(self, reader, writer):
        data = await reader.read(65536)
        self.received.append(data)
        if data.startswith(b"GET "):
            body = b"hello"
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: close\r\n\r\n" + body)
        else:
            writer.write(b"echo:" + data)
        await writer.drain()
        writer.close()

    async def close(self):
        self.server.close()
        await self.server.wait_closed()


@pytest.fixture
async def rig():
    upstream = await Upstream().start()
    answers = {"public.example": [[PUBLIC_IP]], "private.example": [["10.0.0.5"]],
               "mixed.example": [[PUBLIC_IP, "10.0.0.5"]], "rebind.example": [[PUBLIC_IP], ["10.0.0.5"]]}
    lookups: list[str] = []
    connected: list[tuple[str, int]] = []
    events: list[dict] = []

    async def resolve(host):
        lookups.append(host)
        seq = answers.get(host)
        if seq is None:
            raise E.DnsError("nxdomain")
        return seq[0] if len(seq) == 1 else seq.pop(0)

    async def connector(ip, port):
        connected.append((ip, port))
        assert E.is_public_address(ip), ip           # the seam only ever sees checked addresses
        return await asyncio.open_connection("127.0.0.1", upstream.port)

    proxy = E.EgressProxy(resolver=resolve, connector=connector, on_event=events.append)
    port = await proxy.start()
    yield NS(proxy=proxy, port=port, upstream=upstream, lookups=lookups, connected=connected, events=events)
    await proxy.close()
    await upstream.close()


async def _raw(port, data: bytes, then: bytes = b"") -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(data)
    await writer.drain()
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
    if head.startswith(b"HTTP/1.1 200 Connection Established") and then:
        writer.write(then)
        await writer.drain()
    rest = await asyncio.wait_for(reader.read(), 5)
    writer.close()
    return head + rest


async def test_connect_to_a_public_host_is_tunneled_to_the_checked_address(rig):
    out = await _raw(rig.port, b"CONNECT public.example:443 HTTP/1.1\r\nHost: public.example:443\r\n\r\n",
                     then=b"tls bytes")
    assert out.startswith(b"HTTP/1.1 200 Connection Established") and out.endswith(b"echo:tls bytes")
    assert rig.connected == [(PUBLIC_IP, 443)]


async def test_plain_http_is_rewritten_to_origin_form_with_connection_close(rig):
    out = await _raw(rig.port, b"GET http://public.example/a/b?c=1 HTTP/1.1\r\nHost: 10.0.0.5\r\n"
                               b"Proxy-Connection: keep-alive\r\nProxy-Authorization: x\r\nConnection: X-Drop\r\n"
                               b"X-Drop: 1\r\nAccept: */*\r\n\r\n")
    assert out.startswith(b"HTTP/1.1 200 OK") and out.endswith(b"hello")
    sent = rig.upstream.received[0].decode()
    assert sent.startswith("GET /a/b?c=1 HTTP/1.1\r\n")
    assert "Connection: close" in sent and "Accept: */*" in sent
    assert "Host: public.example\r\n" in sent and "10.0.0.5" not in sent
    for header in ("Proxy-Connection", "Proxy-Authorization", "X-Drop"):
        assert header not in sent


@pytest.mark.parametrize(("target", "reason"), [
    ("192.168.1.1:443", "private"), ("10.0.0.5:443", "private"), ("[fd00::1]:443", "private"),
    ("[::ffff:10.0.0.5]:443", "private"), ("0.0.0.0:443", "unspecified"), ("[64:ff9b::a00:5]:443", "private"),
    ("167772165:443", "private"), ("0xA000005:443", "private"), ("router.local:443", "internal_name"),
    ("localhost:443", "localhost"), ("127.0.0.1:443", "loopback"), ("[::1]:443", "loopback"),
    ("private.example:443", "dns_private"), ("mixed.example:443", "dns_private"),
    ("public.example:25", "restricted_port"), ("public.example", "port"), ("intranet:443", "single_label"),
])
async def test_connect_to_private_targets_is_refused_before_any_connection(rig, target, reason):
    out = await _raw(rig.port, f"CONNECT {target} HTTP/1.1\r\n\r\n".encode())
    status = out.split(b"\r\n", 1)[0]
    assert status in (b"HTTP/1.1 403 Forbidden", b"HTTP/1.1 400 Bad Request")
    assert f"reason={reason}".encode() in out
    assert rig.connected == []
    assert rig.events and rig.events[-1]["reason"] == reason


async def test_plain_http_to_private_targets_and_other_schemes_are_refused(rig):
    for url, reason in (("http://192.168.1.1/admin?x=1", "private"), ("http://167772165/", "private"),
                        ("http://0xA000005/", "private"), ("http://router.local/", "internal_name"),
                        ("ftp://public.example/", "scheme"), ("https://public.example/", "scheme")):
        out = await _raw(rig.port, f"GET {url} HTTP/1.1\r\n\r\n".encode())
        assert f"reason={reason}".encode() in out, url
    assert rig.connected == []
    # Events never carry a path or a query.
    assert all(set(e) == {"event", "kind", "host", "port", "reason"} for e in rig.events)
    assert "admin" not in json.dumps(rig.events)


async def test_dns_is_rechecked_for_every_connection_so_rebinding_fails(rig):
    first = await _raw(rig.port, b"CONNECT rebind.example:443 HTTP/1.1\r\n\r\n", then=b"x")
    second = await _raw(rig.port, b"CONNECT rebind.example:443 HTTP/1.1\r\n\r\n", then=b"x")
    assert first.startswith(b"HTTP/1.1 200")
    assert b"reason=dns_private" in second
    assert rig.lookups.count("rebind.example") == 2
    assert rig.connected == [(PUBLIC_IP, 443)]


async def test_dns_failure_and_garbage_requests(rig):
    out = await _raw(rig.port, b"CONNECT nx.example:443 HTTP/1.1\r\n\r\n")
    assert out.startswith(b"HTTP/1.1 502") and b"reason=dns_failed" in out
    out = await _raw(rig.port, b"NONSENSE\r\n\r\n")
    assert out.startswith(b"HTTP/1.1 400")
    out = await _raw(rig.port, b"GET / HTTP/2.0\r\n\r\n")
    assert out.startswith(b"HTTP/1.1 400")


async def test_proxy_listens_on_loopback_only():
    proxy = E.EgressProxy(resolver=AsyncMock(return_value=[]))
    with pytest.raises(ValueError):
        await proxy.start(host="0.0.0.0")


async def test_connection_cap_answers_503():
    gate = asyncio.Event()

    async def slow_resolve(host):
        await gate.wait()
        return [PUBLIC_IP]

    proxy = E.EgressProxy(resolver=slow_resolve, max_connections=1)
    port = await proxy.start()
    try:
        r1, w1 = await asyncio.open_connection("127.0.0.1", port)
        w1.write(b"CONNECT public.example:443 HTTP/1.1\r\n\r\n")
        await w1.drain()
        await asyncio.sleep(0.1)
        out = await _raw(port, b"CONNECT public.example:443 HTTP/1.1\r\n\r\n")
        assert out.startswith(b"HTTP/1.1 503")
        w1.close()
    finally:
        gate.set()
        await proxy.close()


# Office-owned public addresses (review WI-8) ------------------------------------------------------


OFFICE = E.parse_networks(["203.0.113.7/32", "2001:db8:1200::/56"])


def test_office_ranges_are_refused_in_every_form():
    assert E.parse_networks(["203.0.113.7", "bogus", None, "2001:db8:1200::/56"]) == OFFICE
    for text in ("203.0.113.7", "::ffff:203.0.113.7", "64:ff9b::cb00:7107", "2002:cb00:7107::1",
                 "2001:db8:1200:5::10"):
        assert E.address_refusal(text, OFFICE) == "office", text
    assert E.address_refusal("203.0.113.7") != "office"      # without the list: the plain floor only
    assert E.address_refusal(PUBLIC_IP, OFFICE) is None
    assert "office" in E.REFUSAL_REASONS


async def test_proxy_refuses_office_wan_ip_literal_and_names_resolving_to_it():
    connected = []
    office_ip = PUBLIC_IP
    nets = E.parse_networks([f"{office_ip}/32"])

    async def resolve(host):
        return [office_ip]

    async def connector(ip, port):
        connected.append(ip)
        raise OSError("must not connect")

    events = []
    proxy = E.EgressProxy(resolver=resolve, connector=connector, on_event=events.append, extra_blocked=nets)
    port = await proxy.start()
    try:
        out = await _raw(port, f"CONNECT {office_ip}:8443 HTTP/1.1\r\n\r\n".encode())
        assert out.startswith(b"HTTP/1.1 403") and b"reason=office" in out
        out = await _raw(port, b"GET http://router.example.com/admin HTTP/1.1\r\nHost: router.example.com\r\n\r\n")
        assert out.startswith(b"HTTP/1.1 403") and b"reason=office" in out
    finally:
        await proxy.close()
    assert connected == [] and {e["reason"] for e in events} == {"office"}


async def test_user_job_proxy_gets_the_office_ranges_from_floor_json(tmp_path):
    FakeEgress.instances.clear()
    provider, layout, _ctx, _launch = user_provider(tmp_path)
    layout.firewall_dir.mkdir(parents=True, exist_ok=True)
    (layout.firewall_dir / "floor.json").write_text(json.dumps({"extra_blocked": ["203.0.113.7/32"]}))
    await provider.new_context(user_job())
    assert FakeEgress.instances[-1].extra_blocked == ("203.0.113.7/32",)
    cmd = E.EgressProcess(job_id="j", marker=tmp_path, extra_blocked=["203.0.113.7", "2001:db8::/48"]).command()
    assert cmd[-4:] == ["--extra-blocked", "203.0.113.7/32", "--extra-blocked", "2001:db8::/48"]


async def test_egress_child_refuses_office_ranges(tmp_path):
    proc = E.EgressProcess(job_id=str(uuid4()), marker=tmp_path / "job", extra_blocked=["8.8.4.4/32"])
    server = await proc.start()
    try:
        async with httpx.AsyncClient(proxy=server, trust_env=False) as client:
            resp = await client.get("http://8.8.4.4/x")
        assert resp.status_code == 403 and resp.headers["x-bdr-egress"] == "refused; reason=office"
    finally:
        await proc.stop()


def test_egress_child_module_does_not_import_the_agent_stack():
    """Review WI-11: the proxy is the security floor; an import error in an
    unrelated agent must not stop it, and each job should not pay for it."""
    import subprocess
    import sys

    code = ("import sys, bdr_agent.agents.user_browser_v2.egress; "
            "bad = [m for m in sys.modules if m.startswith(('bdr_agent.agents.rfi_from_email', "
            "'bdr_agent.agents.pm_apps', 'bdr_agent.agents.canary', 'bdr_agent.browser', "
            "'bdr_agent.agents.user_browser_v2.guard', 'playwright'))]; print(bad); assert not bad")
    proc = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60,
                          cwd=str(tmp_dir_outside_repo()))
    assert proc.returncode == 0, proc.stdout + proc.stderr


def tmp_dir_outside_repo():
    import tempfile
    from pathlib import Path

    return Path(tempfile.gettempdir())


def test_agents_registry_is_still_built_on_first_access():
    from bdr_agent import agents

    assert {"rfi_from_email", "canary", "user_browser"} <= set(agents.REGISTRY)
    assert agents.REGISTRY is agents.REGISTRY
    with pytest.raises(AttributeError):
        agents.NOT_A_NAME  # noqa: B018


# The child process ---------------------------------------------------------------------------------


async def test_egress_child_process_starts_refuses_and_stops(tmp_path):
    events = []
    proc = E.EgressProcess(job_id=str(uuid4()), marker=tmp_path / "job", on_event=events.append)
    server = await proc.start()
    try:
        assert re.fullmatch(r"http://127\.0\.0\.1:\d+", server) and proc.alive
        assert proc.playwright_proxy == {"server": server}
        assert str(tmp_path / "job") in " ".join(proc.command())
        async with httpx.AsyncClient(proxy=server, trust_env=False) as client:
            resp = await client.get("http://10.0.0.5/private?token=abc")
        assert resp.status_code == 403 and resp.headers["x-bdr-egress"] == "refused; reason=private"
        for _ in range(50):
            if events:
                break
            await asyncio.sleep(0.05)
        assert events[0]["host"] == "10.0.0.5" and "token" not in json.dumps(events)
    finally:
        await proc.stop()
    assert not proc.alive and proc.returncode is not None


async def test_egress_child_exits_when_its_parent_goes_away(tmp_path):
    proc = E.EgressProcess(job_id=str(uuid4()), marker=tmp_path)
    await proc.start()
    child = proc._proc
    child.stdin.close()                      # what the OS does when the worker dies
    await asyncio.wait_for(child.wait(), 10)
    await proc.stop()


async def test_egress_start_failure_is_reported(tmp_path):
    proc = E.EgressProcess(job_id=str(uuid4()), marker=tmp_path, python="/nonexistent/python")
    with pytest.raises((E.EgressStartError, OSError)):
        await proc.start()


# Per-job browser (mocked Playwright) --------------------------------------------------------------


class FakeEgress:
    instances: list["FakeEgress"] = []

    def __init__(self, *, job_id, marker, on_event=None, extra_blocked=()):
        self.job_id, self.marker, self.on_event = job_id, marker, on_event
        self.extra_blocked = tuple(extra_blocked)
        self.started = self.stopped = False
        FakeEgress.instances.append(self)

    async def start(self):
        self.started = True
        return "http://127.0.0.1:9"

    @property
    def playwright_proxy(self):
        return {"server": "http://127.0.0.1:9"}

    @property
    def alive(self):
        return self.started and not self.stopped

    async def stop(self):
        self.stopped = True


class FakeSelfTest:
    def __init__(self, allowed=True):
        self.allowed = allowed
        self.checks = 0

    async def check_before_claim(self):
        self.checks += 1
        return self.allowed

    def require_claims(self):
        if not self.allowed:
            raise EgressFloorUnavailable("floor not proven")


def user_input():
    admission = str(uuid4())
    return {"admission_id": admission, "goal": "Read the open RFIs", "start_url": "https://app.example.com/",
            "authority": {"role": "Executive", "conversation_id": str(uuid4()), "max_cost_usd": 5,
                          "admission_id": admission}}


def user_job(agent_type="user_browser", principal="user"):
    return ClaimedJob(job_id=uuid4(), agent_type=agent_type, principal_kind=principal,
                      principal_user_id=uuid4() if principal == "user" else None, epoch=1, attempts=1,
                      lease_expires_at=datetime.now(UTC) + timedelta(minutes=3),
                      input=user_input() if agent_type == "user_browser" else {})


def mock_context():
    page = NS(is_closed=lambda: False)
    ctx = NS(pages=[page], set_default_timeout=Mock(), set_default_navigation_timeout=Mock(),
             route=AsyncMock(), unroute=AsyncMock(), close=AsyncMock(), request=object(),
             new_page=AsyncMock(return_value=page))
    return ctx


def user_provider(tmp_path, *, allowed=True, guard_factory=None, launch_error=None):
    layout = UserPoolLayout.for_home(tmp_path / "BDRAgentUser", quarantine_root=tmp_path / "BDRQuarantine")
    layout.ensure()
    up = B.UserPoolConfig(layout=layout, selftest=FakeSelfTest(allowed), guard_factory=guard_factory,
                          egress_factory=FakeEgress)
    provider = B.ChromiumProvider(ceilings=lambda *_: None, user_pool=up)
    ctx = mock_context()
    launch = AsyncMock(return_value=ctx, side_effect=launch_error)
    provider._pw = NS(chromium=NS(launch_persistent_context=launch))
    provider._user_chrome_version = "130.0.6723.31"
    return provider, layout, ctx, launch


async def test_every_user_job_rechecks_the_floor_before_launching(tmp_path):
    provider, _layout, _ctx, launch = user_provider(tmp_path)
    await provider.new_context(user_job())
    assert provider.user_pool.selftest.checks == 1
    launch.assert_awaited_once()


async def test_floor_lost_after_boot_stops_new_user_launches(tmp_path):
    """Review WI-8: boot proved the floor, then the rules were deleted (the
    gateway connect now succeeds). The next job must not launch Chromium."""
    from bdr_agent.agents.user_browser_v2 import selftest as S

    provider, layout, _ctx, launch = user_provider(tmp_path)
    layout.firewall_dir.mkdir(parents=True, exist_ok=True)
    (layout.firewall_dir / "floor.json").write_text('{"gateway": "192.168.1.1", "gateway_ports": [80]}')
    st = S.UserPoolSelfTest(layout, platform="win32", connect=lambda h, p, t: None)
    st.report = S.SelfTestReport(at=None, platform="win32", claims_allowed=True)
    provider.user_pool.selftest = st
    with pytest.raises(SetupUnavailable):
        await provider.new_context(user_job())
    launch.assert_not_awaited()
    assert st.claims_allowed is False


async def test_user_job_gets_its_own_sandboxed_chromium_behind_its_own_proxy(tmp_path):
    FakeEgress.instances.clear()
    provider, layout, ctx, launch = user_provider(tmp_path)
    job = user_job()
    jb = await provider.new_context(job)
    kw = launch.await_args.kwargs
    assert kw["user_data_dir"] == str(layout.job_dir(job.job_id) / "profile")
    assert kw["channel"] == "chromium" and kw["chromium_sandbox"] is True
    assert kw["proxy"] == {"server": "http://127.0.0.1:9"}
    assert kw["downloads_path"] == str(layout.quarantine_dir(job.job_id)) and kw["accept_downloads"] is True
    assert kw["service_workers"] == "block" and kw["permissions"] == []
    assert "HeadlessChrome" not in kw["user_agent"] and "Windows NT 10.0" in kw["user_agent"]
    assert "Chrome/130.0.0.0" in kw["user_agent"]
    args = kw["args"]
    assert "--force-webrtc-ip-handling-policy=disable_non_proxied_udp" in args
    assert "--dns-prefetch-disable" in args and "--disable-quic" in args and "--no-sandbox" not in args
    assert sum(a.startswith("--disable-features=") for a in args) == 1
    assert kw["env"]["TMP"] == str(layout.job_dir(job.job_id) / "tmp")
    assert "PLAYWRIGHT_DISABLE_FORCED_CHROMIUM_PROXIED_LOOPBACK" not in kw["env"]
    assert FakeEgress.instances[-1].marker == str(layout.job_dir(job.job_id))
    # Deny-all until the guard is installed; no page for the model before that.
    ctx.route.assert_awaited_once_with("**/*", B._deny_all_route)
    with pytest.raises(RuntimeError):
        await jb.model_page()
    guard = NS(install=AsyncMock(), settle=AsyncMock())
    await jb.install_guard(guard)
    guard.install.assert_awaited_once_with(ctx)
    ctx.unroute.assert_awaited_once_with("**/*", B._deny_all_route)
    assert await jb.model_page() is ctx.pages[0]
    assert jb.request is ctx.request and layout.job_dir(job.job_id).is_dir()
    await provider.close_context(jb)
    guard.settle.assert_awaited_once()
    ctx.close.assert_awaited_once()
    assert FakeEgress.instances[-1].stopped
    assert not layout.job_dir(job.job_id).exists() and not layout.quarantine_dir(job.job_id).exists()


async def test_deny_all_route_aborts():
    route = NS(abort=AsyncMock())
    await B._deny_all_route(route)
    route.abort.assert_awaited_once_with("blockedbyclient")


async def test_guard_factory_installs_at_launch_and_on_relaunch(tmp_path):
    guard = NS(install=AsyncMock())

    async def factory(job, jb):
        return guard

    provider, _layout, ctx, launch = user_provider(tmp_path, guard_factory=factory)
    jb = await provider.new_context(user_job())
    assert jb.guard is guard and guard.install.await_count == 1
    await jb.relaunch(headless=False)
    assert launch.await_args.kwargs["headless"] is False and guard.install.await_count == 2
    await jb.close()


async def test_user_job_refused_without_a_proven_floor_and_never_launches(tmp_path):
    FakeEgress.instances.clear()
    provider, _layout, _ctx, launch = user_provider(tmp_path, allowed=False)
    with pytest.raises(SetupUnavailable):
        await provider.new_context(user_job())
    launch.assert_not_awaited()
    assert FakeEgress.instances == []


async def test_user_job_principal_checks(tmp_path):
    provider, *_ = user_provider(tmp_path)
    with pytest.raises(ValueError):
        await provider.new_context(user_job(principal="org"))
    bare = B.ChromiumProvider(ceilings=lambda *_: None)
    bare._pw = object()
    with pytest.raises(RuntimeError):
        await bare.new_context(user_job())


async def test_launch_failure_stops_the_proxy_and_removes_the_folders(tmp_path):
    FakeEgress.instances.clear()
    provider, layout, _ctx, _launch = user_provider(tmp_path, launch_error=RuntimeError("no chrome"))
    job = user_job()
    with pytest.raises(RuntimeError):
        await provider.new_context(job)
    assert FakeEgress.instances[-1].stopped
    assert not layout.job_dir(job.job_id).exists() and not layout.quarantine_dir(job.job_id).exists()


async def test_org_jobs_keep_the_shared_browser(tmp_path):
    provider, *_ = user_provider(tmp_path)
    ctx = NS(set_default_timeout=Mock(), set_default_navigation_timeout=Mock(), close=AsyncMock())
    shared = NS(new_context=AsyncMock(return_value=ctx))
    provider._ensure_browser = AsyncMock(return_value=shared)
    guard = NS(install=AsyncMock(), new_page=AsyncMock(return_value=NS()))
    import bdr_agent.browser.browser as module
    original = module.Guard
    module.Guard = lambda *_a, **_k: guard
    try:
        jb = await provider.new_context(user_job("rfi_from_email", "org"))
    finally:
        module.Guard = original
    assert isinstance(jb, B.JobBrowser)
    assert shared.new_context.await_args.kwargs["accept_downloads"] is False


def test_windows_user_agent_and_job_object_off_windows():
    assert B.windows_user_agent("131.0.6778.33").endswith("Chrome/131.0.0.0 Safari/537.36")
    assert B.windows_user_agent(None) is None and B.windows_user_agent("HeadlessChrome") is None
    job = B.WindowsJobObject()
    if os.name != "nt":
        assert job.supported is False and job.assign_current_process() is False


# Slot: type checks and several user jobs at once ---------------------------------------------------


class UserRunner:
    needs_browser = False
    version = "1.1.0"

    def __init__(self, started=None, gate=None):
        self.clients = []
        self.started = started
        self.gate = gate

    def user_slot_parts(self, base_client):
        client = NS(base=base_client, finish=AsyncMock(), requeue=AsyncMock(), append_steps=AsyncMock(),
                    call=AsyncMock())
        self.clients.append(client)
        return UserSlotParts(client=client, spool=Spool(base_client.spool_dir))

    async def run(self, ctx):
        if self.started is not None:
            self.started.append(ctx.job_id)
        if self.gate is not None:
            await self.gate.wait()
        ctx.secrets.add("job-secret-" + str(ctx.job_id), owner=str(ctx.job_id))
        return Finish("succeeded", "done", outcome="nothing_to_do")


def slot_deps(tmp_path, runners, browser=None):
    base = NS(spool_dir=tmp_path / "spool", finish=AsyncMock(), requeue=AsyncMock(), call=AsyncMock(),
              append_steps=AsyncMock())
    return SlotDeps(client=base, tracker=JobTracker(), spool=Spool(tmp_path / "spool"), secrets=LiveSecrets(),
                    runners=runners, browser=browser, tick=0.01, flush_interval=0.01), base


TC = TypeConfig(enabled=True, max_concurrency=4, max_steps=10, max_active_seconds=60)


async def test_user_browser_slot_uses_the_runner_adapter_never_the_org_client(tmp_path):
    runner = UserRunner()
    deps, base = slot_deps(tmp_path, {"user_browser": runner})
    out = await Slot("user_browser", 0, deps).run(user_job(), TC)
    assert out.terminal == "finish:succeeded"
    runner.clients[0].finish.assert_awaited_once()
    base.finish.assert_not_awaited()
    assert deps.client is base


async def test_user_browser_slot_refuses_without_adapter_or_with_wrong_principal(tmp_path):
    class NoAdapter:
        needs_browser = False

        async def run(self, ctx):
            raise AssertionError("must not run")

    deps, base = slot_deps(tmp_path, {"user_browser": NoAdapter()})
    out = await Slot("user_browser", 0, deps).run(user_job(), TC)
    assert (out.terminal, out.detail) == ("none", "no_user_adapter")

    class SameClient(UserRunner):
        def user_slot_parts(self, base_client):
            return UserSlotParts(client=base_client, spool=Spool(base_client.spool_dir))

    deps, base = slot_deps(tmp_path, {"user_browser": SameClient()})
    assert (await Slot("user_browser", 0, deps).run(user_job(), TC)).detail == "no_user_adapter"
    deps, base = slot_deps(tmp_path, {"user_browser": UserRunner()})
    assert (await Slot("user_browser", 0, deps).run(user_job(principal="org"), TC)).detail == "principal_mismatch"
    deps, base = slot_deps(tmp_path, {"rfi_from_email": UserRunner()})
    assert (await Slot("rfi_from_email", 0, deps).run(user_job("rfi_from_email"), TC)).detail == "principal_mismatch"
    base.finish.assert_not_awaited()


async def test_three_user_jobs_run_at_once_with_separate_clients_and_secrets(tmp_path):
    gate = asyncio.Event()
    started = []
    runner = UserRunner(started=started, gate=gate)
    deps, _base = slot_deps(tmp_path, {"user_browser": runner})
    jobs = [user_job() for _ in range(3)]
    tasks = [asyncio.create_task(Slot("user_browser", i, deps).run(job, TC)) for i, job in enumerate(jobs)]
    for _ in range(100):
        if len(started) == 3:
            break
        await asyncio.sleep(0.01)
    assert sorted(map(str, started)) == sorted(str(j.job_id) for j in jobs)   # all three running together
    gate.set()
    outs = await asyncio.gather(*tasks)
    assert [o.terminal for o in outs] == ["finish:succeeded"] * 3
    assert len({id(c) for c in runner.clients}) == 3
    for client in runner.clients:
        client.finish.assert_awaited_once()
    for job in jobs:
        assert deps.secrets.scrub_text("job-secret-" + str(job.job_id)) == "job-secret-" + str(job.job_id)


async def test_setup_unavailable_is_an_uncharged_requeue_with_retry_at(tmp_path):
    class Runner(UserRunner):
        needs_browser = True

    browser = NS(new_context=AsyncMock(side_effect=EgressFloorUnavailable("not proven")), close_context=AsyncMock())
    runner = Runner()
    deps, _base = slot_deps(tmp_path, {"user_browser": runner}, browser=browser)
    out = await Slot("user_browser", 0, deps).run(user_job(), TC)
    assert (out.terminal, out.detail) == ("requeue:interrupted", "setup_unavailable")
    req = runner.clients[0].requeue.await_args.args[1]
    assert req.reason == "interrupted" and req.retry_at > datetime.now(UTC) + timedelta(minutes=4)


# Real Chromium behind the real egress child (needs_browser) ------------------------------------------


class PassGuard:
    """Stands in for UserGuard v2: lets every request through, optionally via
    route.fetch (the driver-side path the guard uses for gated requests)."""

    def __init__(self, fetch=False):
        self.fetch = fetch
        self.fetch_results = []

    async def install(self, context):
        await context.route("**/*", self._on_route)

    async def _on_route(self, route):
        if not self.fetch:
            await route.continue_()
            return
        try:
            resp = await route.fetch(max_redirects=0)
            self.fetch_results.append(resp.status)
            await route.fulfill(response=resp)
        except Exception as exc:
            self.fetch_results.append(type(exc).__name__)
            await route.abort()


@pytest.fixture(autouse=True)
def _restore_environ():
    # The user pool provider sets driver variables on the worker process; keep
    # them out of the other tests.
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture
async def loopback_site():
    hits = []

    async def handle(reader, writer):
        hits.append(await reader.read(4096))
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    yield NS(port=server.sockets[0].getsockname()[1], hits=hits)
    server.close()
    await server.wait_closed()


async def _real_provider(tmp_path):
    from bdr_agent.agents.user_browser_v2.selftest import UserPoolSelfTest

    layout = UserPoolLayout.for_home(tmp_path / "BDRAgentUser", quarantine_root=tmp_path / "BDRQuarantine")
    st = UserPoolSelfTest(layout, dev_unenforced=True)
    provider = B.ChromiumProvider(ceilings=lambda *_: None, user_pool=B.UserPoolConfig(layout=layout, selftest=st))
    await provider.start()
    return provider, layout


@needs_browser
async def test_real_chromium_cannot_reach_loopback_or_private_hosts(tmp_path, loopback_site):
    provider, layout = await _real_provider(tmp_path)
    try:
        job = user_job()
        jb = await provider.new_context(job)
        guard = PassGuard()
        await jb.install_guard(guard)
        page = await jb.model_page()
        assert "HeadlessChrome" not in await page.evaluate("navigator.userAgent")
        for url in (f"http://127.0.0.1:{loopback_site.port}/", f"http://localhost:{loopback_site.port}/",
                    "http://167772165/", "http://0xA000005/", "http://192.168.1.1/", "http://router.local/"):
            resp = None
            try:
                resp = await page.goto(url, timeout=10_000)
            except Exception:
                pass
            assert resp is None or resp.status == 403, url
        # The context's APIRequestContext goes through the proxy too (the
        # driver's proxy agent tunnels, so a refusal surfaces as an error).
        try:
            resp = await jb.request.get(f"http://127.0.0.1:{loopback_site.port}/api", timeout=10_000)
            assert resp.status == 403 and "reason=loopback" in resp.headers.get("x-bdr-egress", "")
        except Exception as exc:
            assert "403" in str(exc) and "reason=loopback" in str(exc)
        # Playwright 1.64 returns the refused CONNECT as a 403 response;
        # older drivers raised. Either way the request never left the PC.
        try:
            resp = await jb.request.get("https://10.0.0.5/", timeout=10_000)
            assert resp.status == 403 and "reason=private" in resp.headers.get("x-bdr-egress", "")
        except AssertionError:
            raise
        except Exception as exc:
            assert "403" in str(exc) or "private" in str(exc), exc
        # route.fetch (made by the Playwright driver) goes through the proxy.
        jb.guard = None
        fetch_guard = PassGuard(fetch=True)
        await jb.install_guard(fetch_guard)
        resp = None
        with contextlib.suppress(Exception):     # 1.64: the 403 arrives as a response
            resp = await page.goto(f"http://127.0.0.1:{loopback_site.port}/fetched", timeout=10_000)
        assert resp is None or resp.status == 403
        assert fetch_guard.fetch_results and fetch_guard.fetch_results[-1] in (403, "Error")
        assert loopback_site.hits == []
        assert {e["reason"] for e in jb.egress.refusals} >= {"loopback", "private"}
        await provider.close_context(jb)
        assert not layout.job_dir(job.job_id).exists()
        assert not jb.egress.alive
    finally:
        await provider.stop()


@needs_browser
async def test_real_chromium_webrtc_gathers_no_udp_candidates(tmp_path):
    stun = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    stun.bind(("127.0.0.1", 0))
    stun.setblocking(False)
    provider, _layout = await _real_provider(tmp_path)
    try:
        jb = await provider.new_context(user_job())
        await jb.install_guard(PassGuard())
        page = await jb.model_page()
        port = stun.getsockname()[1]
        candidates = await page.evaluate("""async (port) => {
            const pc = new RTCPeerConnection({iceServers: [{urls: `stun:127.0.0.1:${port}`}]});
            const found = [];
            pc.onicecandidate = (e) => { if (e.candidate) found.push(e.candidate.candidate); };
            pc.createDataChannel('x');
            await pc.setLocalDescription(await pc.createOffer());
            await new Promise((r) => setTimeout(r, 3000));
            pc.close();
            return found;
        }""", port)
        for cand in candidates:
            assert " udp " not in cand.lower() or ".local" in cand, cand
            assert not re.search(r"\b(10|192\.168|172\.(1[6-9]|2\d|3[01]))\.\d+\.\d+", cand), cand
        with pytest.raises(BlockingIOError):
            stun.recvfrom(2048)
        await jb.close()
    finally:
        stun.close()
        await provider.stop()


async def test_user_pool_start_cleans_up_first_then_tests_the_floor(tmp_path, monkeypatch):
    order = []
    layout = UserPoolLayout.for_home(tmp_path / "BDRAgentUser", quarantine_root=tmp_path / "BDRQuarantine")
    node = layout.runtime_dir / "driver" / "node.exe"
    node.parent.mkdir(parents=True)
    node.write_bytes(b"node")
    (layout.runtime_dir / "runtime.json").write_text(json.dumps({"node_exe": "driver/node.exe"}))

    class St:
        def cleanup(self):
            order.append("cleanup")

        async def run_boot(self, probe):
            order.append(("boot", probe))
            return NS(claims_allowed=True, downloads_allowed=False)

    class PwCm:
        async def __aenter__(self):
            order.append(("driver", os.environ.get("PLAYWRIGHT_NODEJS_PATH"),
                          os.environ.get("PLAYWRIGHT_DISABLE_FORCED_CHROMIUM_PROXIED_LOOPBACK"),
                          os.environ.get("PLAYWRIGHT_CHROMIUM_USE_HEADLESS_NEW")))
            return NS(chromium=NS())

    import playwright.async_api as pw_api
    monkeypatch.setattr(pw_api, "async_playwright", lambda: PwCm())
    monkeypatch.setenv("PLAYWRIGHT_DISABLE_FORCED_CHROMIUM_PROXIED_LOOPBACK", "1")
    provider = B.ChromiumProvider(ceilings=lambda *_: None, user_pool=B.UserPoolConfig(layout=layout, selftest=St()))
    await provider.start()
    assert order[0] == "cleanup"
    assert order[1] == ("driver", str(node.resolve()), None, None)   # no 1.48 headless switch on 1.64
    assert order[2] == ("boot", provider.chromium_probe)
    assert provider._browser is None          # no shared Chromium in the user pool


async def test_chromium_probe_connects_directly_with_the_real_executable(tmp_path):
    provider, *_ = user_provider(tmp_path)
    page = NS(goto=AsyncMock(side_effect=RuntimeError("net::ERR_NETWORK_ACCESS_DENIED at http://192.168.1.1/")))
    browser = NS(version="131.0.1.2", new_page=AsyncMock(return_value=page), close=AsyncMock())
    launch = AsyncMock(return_value=browser)
    provider._pw = NS(chromium=NS(launch=launch))
    exe = tmp_path / "chrome.exe"
    error = await provider.chromium_probe(exe, "http://192.168.1.1:80/")
    assert "ERR_NETWORK_ACCESS_DENIED" in error and provider._user_chrome_version == "131.0.1.2"
    kw = launch.await_args.kwargs
    assert kw["executable_path"] == str(exe) and "--no-proxy-server" in kw["args"]
    assert kw["chromium_sandbox"] is True and "proxy" not in kw
    browser.close.assert_awaited_once()
