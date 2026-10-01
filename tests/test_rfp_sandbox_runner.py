"""The parent runner of the RFP Ingestion Sandbox, exercised against FAKE children.

Everything here spawns a real subprocess, but never the real child: each test
builds a throwaway repo root (app/__init__.py, app/sandbox/__init__.py, the
REAL protocol.py copied in, and app/sandbox/__main__.py = the scripted fake
below), and points `run_sandbox(repo_root=...)` at it. The fake child is driven
by a JSON script that the test writes as the "PDF" input file (the runner never
sniffs bytes; that is rfp_ingest's job), so a test can make the child emit any
event sequence, write any file (real Pillow JPEGs, symlinks, FIFOs, sparse
giants), crash by signal or exit code, hang, or linger. The verify spawn reads
the same script back from out/tmp (the child's own scratch, never read by the
parent).

Invariants pinned, layer by layer:

- Launch: the exact argv from protocol.py (`-I -X utf8 -c BOOTSTRAP <repo>
  --spawn N ...`, `--skip-file` only on a respawn, `--verify ... --list-file`),
  cwd = out, a three-variable environment, DEVNULL stdin/stdout, a file fd for
  stderr, close_fds, start_new_session, and user/group/extra_groups when a
  UidSlot is given.
- Monitoring: incremental progress parsing (on_progress sees pages_done grow
  while the child runs), a torn trailing line is discarded after a crash but is
  invalid_output after a clean exit, the post-exit drain reading the whole log
  rather than one read window, open timeout (heartbeats do not extend it),
  page stall, file wall clock, disk quota by bytes AND by out-dir entry count
  (a zero-byte file bomb), run page budget, cancel, lease loss (LeaseLost
  raised, child dead, nothing returned).
- Crash attribution per the protocol docstring: no start / no ready /
  EXIT_BAD_ARGS = failed/spawn; ready without document = rejected/unreadable;
  dangling page_start blamed (crash, memory when limits_applied.memory and a
  SIGKILL/SIGABRT/non-zero exit, stall when the parent killed it); a crash with
  no dangling page_start blames the first uncovered index; respawn only when
  the skip list grew; restart cap; a respawn's document must repeat page_count;
  a reject on a respawn is rejected/unreadable; end.aborted = resource_limit
  with the uncovered pages recorded failed/aborted.
- The page sink: every ok page of a complete file delivered once, in index
  order, with bytes that match the digest validation recorded and no bytes left
  on the result; nothing delivered for a rejected, failed or verify-failed
  page; an artifact changed between validation and delivery failing the whole
  file as invalid_output; and an exception from the sink reaching the caller
  with the scratch tree still removed.
- Output integrity: every listed rejection (bad JSON, NaN, deep nesting,
  unknown event, wrong sha, wrong dims, extra file, symlinked file, symlinked
  subdir, FIFO, `..` in a name, non-JPEG bytes, disallowed marker, trailing
  bytes, index out of range, duplicate index, oversized artifact, line caps)
  is failed/invalid_output with an EMPTY pages list, and the artifacts are
  otherwise returned as validated bytes with parent-re-sanitized text; plus
  `_parse_line` on its own, and a child that plants the name the parent will
  derive for the next spawn (its own file's fault, never a spawn error).
- Verify spawn and the gap rule, including the verify pass having its own wall
  clock (an expired FILE clock never discards a rendered file, and running out
  of the verify budget only fails the pages it did not reach), and the stderr
  tail carrying both the last process spawn and the always-noisy verify spawn.
- uid slots (flock, occupancy, release, the bounded wait) with a faked root
  euid, scratch cleanup, and the embedded self-test PDF.

Timeouts are 1 to 5 s so the file runs in about a minute; the monitor tick is
shortened through the module constant (the cadence contract itself is not
under test here). The last tests run the REAL child when it exists: the happy
path, the page sink, a per-page failure, and every reject verdict, so drift
between what the real child emits and what the parent accepts cannot hide
behind the scripted fake.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.sandbox import protocol
from app.services import rfp_sandbox_runner as runner
from app.services.rfp_sandbox_runner import (
    LeaseLost,
    SandboxLimits,
    SandboxResult,
    SandboxSpawnError,
    UidSlot,
)

# ── The scripted fake child ─────────────────────────────────────────────────

FAKE_CHILD = r'''
"""Fake sandbox child: replays a JSON script handed over as the --input file."""
import argparse
import base64
import hashlib
import io
import json
import os
import signal
import sys
import time

from app.sandbox import protocol


def _jpeg(w, h, seed=0):
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), ((seed * 37) % 256, 80, 120)).save(buf, "JPEG", quality=70)
    return buf.getvalue()


def _crash(how):
    if how == "exit":
        os._exit(1)
    os.kill(os.getpid(), {"segv": signal.SIGSEGV, "kill": signal.SIGKILL,
                          "abrt": signal.SIGABRT}[how])
    time.sleep(5)


class Child:
    def __init__(self, args):
        self.args = args
        self.out = args.out
        self.log_fd = None
        with open(args.limits) as f:
            self.limits = json.load(f)
        self.skip = set()
        if args.skip_file:
            with open(args.skip_file) as f:
                self.skip = {int(x) for x in f.read().split()}

    def emit(self, obj):
        os.write(self.log_fd, (json.dumps(obj) + "\n").encode("utf-8"))

    def raw(self, text):
        os.write(self.log_fd, text.encode("utf-8", "surrogateescape"))

    def start_event(self, ov):
        return {"event": "start", "sandbox_version": protocol.SANDBOX_VERSION,
                "protocol_version": protocol.PROTOCOL_VERSION, "spawn": self.args.spawn,
                "pid": os.getpid(), "uid": os.getuid(), "gid": os.getgid(),
                "limits": self.limits,
                "limits_applied": {"memory": bool(ov.get("memory_applied", False)),
                                   "cpu": True, "fsize": True, "nofile": True,
                                   "nproc": True, "core": True},
                "skip_count": len(self.skip)}

    def ready_event(self):
        return {"event": "ready", "versions": {"python": "3.12", "pypdfium2": "fake",
                "pdfium": "0", "pillow": "fake"}, "pdfium_flags": None,
                "platform": sys.platform}

    def document_event(self, ov):
        doc = {"event": "document", "page_count": 1, "pdf_version": "1.4",
               "owner_restricted": False, "security_handler_revision": None,
               "form_type": "none", "metadata": {"title": "Fake ​ title"},
               "hazards": {"javascript_actions": 0, "attachments": 0, "xfa_packets": 0}}
        doc.update(ov)
        return doc

    def end_event(self, ov):
        end = {"event": "end", "pages_ok": 0, "pages_failed": 0, "elapsed_ms": 5,
               "peak_rss_kb": 4321, "output_bytes": 0, "aborted": None}
        end.update(ov)
        return end

    def write_file(self, spec):
        path = os.path.join(self.out, spec["path"])
        os.makedirs(os.path.dirname(path), exist_ok=True)
        kind = spec.get("kind", "text")
        if kind == "text":
            with open(path, "w", encoding="utf-8") as f:
                f.write(spec.get("data", ""))
        elif kind == "b64":
            with open(path, "wb") as f:
                f.write(base64.b64decode(spec["data"]))
        elif kind == "sparse":
            with open(path, "wb") as f:
                f.truncate(spec["size"])
        elif kind == "symlink":
            os.symlink(spec["target"], path)
        elif kind == "fifo":
            os.mkfifo(path)
        elif kind == "dir":
            os.makedirs(path, exist_ok=True)
        elif kind == "jpeg":
            with open(path, "wb") as f:
                f.write(_jpeg(spec["w"], spec["h"]))

    def write_many(self, spec):
        """N zero-byte files: the byte sum stays at 0, so only the out-dir
        entry cap can stop this."""
        base = os.path.join(self.out, spec.get("dir", protocol.TMP_DIR))
        os.makedirs(base, exist_ok=True)
        for i in range(spec["count"]):
            os.close(os.open(os.path.join(base, "f%06d" % i),
                             os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))

    def page_ok(self, i, ov):
        lim = self.limits
        tw, th = ov.get("thumb_dims", [lim["thumb_long_side"], max(1, lim["thumb_long_side"] // 2)])
        fw, fh = ov.get("full_dims", [lim["full_small_long_side"],
                                      max(1, lim["full_small_long_side"] // 2)])
        thumb = base64.b64decode(ov["thumb_b64"]) if "thumb_b64" in ov else _jpeg(tw, th, i)
        full = base64.b64decode(ov["full_b64"]) if "full_b64" in ov else _jpeg(fw, fh, i + 100)
        text = ov.get("text", "page %d text\n" % i).encode("utf-8")
        files = {
            "thumb": (protocol.page_file(protocol.THUMB_DIR, i, "jpg"), thumb),
            "full": (protocol.page_file(protocol.FULL_DIR, i, "jpg"), full),
            "text": (protocol.page_file(protocol.TEXT_DIR, i, "txt"), text),
        }
        for kind, (rel, data) in files.items():
            if kind in ov.get("omit", []):
                continue
            path = os.path.join(self.out, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(data)

        def meta(rel, data, w, h):
            return {"file": rel, "w": w, "h": h, "bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest()}

        ev = {"event": "page", "index": i, "status": "ok", "width_pt": 612.0,
              "height_pt": 792.0, "rotation": 0, "tier": "full_small",
              "thumb": meta(files["thumb"][0], thumb, tw, th),
              "full": meta(files["full"][0], full, fw, fh),
              "text": {"file": files["text"][0],
                       "chars": min(len(text), lim["max_text_chars_per_page"]),
                       "truncated": False, "sha256": hashlib.sha256(text).hexdigest(),
                       "hazards": ov.get("text_hazards", {})},
              "hazards": ov.get("hazards", {}), "render_ms": 3}
        for key in ("thumb", "full", "text"):
            if key + "_meta" in ov:
                ev[key].update(ov[key + "_meta"])
        ev.update(ov.get("event", {}))
        if not ov.get("no_start"):
            self.emit({"event": "page_start", "index": i})
        self.emit(ev)

    def run_steps(self, steps):
        for step in steps:
            if "sleep" in step:
                time.sleep(step["sleep"])
            elif "start" in step:
                self.emit(self.start_event(step["start"]))
            elif "ready" in step:
                self.emit(self.ready_event())
            elif "heartbeat" in step:
                self.emit({"event": "heartbeat", "phase": "open", "elapsed_ms": 1})
            elif "heartbeats" in step:
                for _ in range(step["heartbeats"]):
                    self.emit({"event": "heartbeat", "phase": "open", "elapsed_ms": 1})
            elif "heartbeat_loop" in step:
                while True:
                    self.emit({"event": "heartbeat", "phase": "open", "elapsed_ms": 1})
                    time.sleep(step["heartbeat_loop"])
            elif "document" in step:
                self.emit(self.document_event(step["document"]))
            elif "reject" in step:
                self.emit({"event": "reject", **step["reject"]})
            elif "page_start" in step:
                self.emit({"event": "page_start", "index": step["page_start"]})
            elif "page_ok" in step:
                self.page_ok(step["page_ok"], step.get("overrides", {}))
            elif "page_failed" in step:
                i = step["page_failed"]
                if not step.get("no_start"):
                    self.emit({"event": "page_start", "index": i})
                self.emit({"event": "page", "index": i, "status": "failed",
                           "code": step.get("code", "render_error"),
                           "detail": step.get("detail", "boom")})
            elif "end" in step:
                self.emit(self.end_event(step["end"]))
            elif "raw" in step:
                self.raw(step["raw"])
            elif "event" in step:
                self.emit(step["event"])
            elif "write" in step:
                self.write_file(step["write"])
            elif "many" in step:
                self.write_many(step["many"])
            elif "stderr" in step:
                sys.stderr.write(step["stderr"])
                sys.stderr.flush()
            elif "exit" in step:
                os._exit(step["exit"])
            elif "crash" in step:
                _crash(step["crash"])
            elif "hang" in step:
                time.sleep(3600)

    def auto(self, spec):
        n = spec["pages"]
        self.emit(self.start_event({"memory_applied": spec.get("memory_applied", False)}))
        self.emit(self.ready_event())
        self.emit(self.document_event({"page_count": spec.get("page_count", n)}))
        ok = failed = 0
        first = True
        for i in range(n):
            if i in self.skip:
                continue
            if i == spec.get("hang_at"):
                self.emit({"event": "page_start", "index": i})
                time.sleep(3600)
            if i == spec.get("crash_at") or (first and spec.get("crash_first")):
                self.emit({"event": "page_start", "index": i})
                if spec.get("partial"):
                    self.write_file({"path": protocol.page_file(protocol.THUMB_DIR, i, "jpg"),
                                     "kind": "text", "data": "partial"})
                if spec.get("torn"):
                    self.raw('{"event": "page", "index": %d, "status": "ok", "wid' % i)
                _crash(spec.get("crash_how", "exit"))
            first = False
            if i in spec.get("fail", []):
                self.emit({"event": "page_start", "index": i})
                self.emit({"event": "page", "index": i, "status": "failed",
                           "code": "render_error", "detail": spec.get("detail", "boom")})
                failed += 1
                continue
            self.page_ok(i, spec.get("overrides", {}).get(str(i), {}))
            ok += 1
            if spec.get("page_delay"):
                time.sleep(spec["page_delay"])
        if spec.get("crash_before_end"):
            _crash(spec.get("crash_how", "exit"))
        if spec.get("no_end"):
            return
        self.emit(self.end_event({"pages_ok": ok, "pages_failed": failed,
                                  "aborted": spec.get("aborted")}))
        if spec.get("linger"):
            time.sleep(3600)


def verify(args):
    # The real child writes this line unconditionally, before anything else.
    sys.stderr.write("sandbox verify: limits applied core=1,cpu=1\n")
    sys.stderr.flush()
    with open(os.path.join(args.out, protocol.TMP_DIR, "script.json")) as f:
        script = json.load(f)
    v = script.get("verify", {})
    if v.get("exit_before_log") is not None:
        os._exit(v["exit_before_log"])
    with open(args.list_file) as f:
        names = [line.strip() for line in f if line.strip()]
    fd = os.open(os.path.join(args.out, protocol.VERIFY_FILE),
                 os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND, 0o644)
    if v.get("raw"):
        os.write(fd, v["raw"].encode("utf-8"))
    from PIL import Image

    def line(name, w, h, ok):
        os.write(fd, (json.dumps({"event": "verified", "file": name, "w": w, "h": h,
                                  "ok": ok}) + "\n").encode("utf-8"))

    for name in names:
        if name in v.get("omit", []):
            continue
        try:
            with Image.open(os.path.join(args.out, name)) as im:
                im.load()
                w, h = im.size
            ok = True
        except Exception:
            w = h = 0
            ok = False
        if name in v.get("fail", []):
            ok = False
        if name in v.get("dims", {}):
            w, h = v["dims"][name]
        line(name, w, h, ok)
        if v.get("delay"):
            time.sleep(v["delay"])
    for name in v.get("extra", []):
        line(name, 1, 1, True)
    if v.get("hang"):
        time.sleep(3600)
    os._exit(v.get("exit_code", 0))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--spawn", type=int, default=0)
    ap.add_argument("--input")
    ap.add_argument("--out", required=True)
    ap.add_argument("--limits", required=True)
    ap.add_argument("--skip-file")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--list-file")
    args = ap.parse_args()
    if args.verify:
        verify(args)
        return
    with open(args.input) as f:
        script = json.load(f)
    with open(os.path.join(args.out, protocol.TMP_DIR, "script.json"), "w") as f:
        json.dump(script, f)
    spawns = script.get("spawns") or [script]
    spec = spawns[min(args.spawn, len(spawns) - 1)]
    if spec.get("stderr"):
        sys.stderr.write(spec["stderr"])
        sys.stderr.flush()
    if spec.get("exit_before_log") is not None:
        os._exit(spec["exit_before_log"])
    child = Child(args)
    child.log_fd = os.open(os.path.join(args.out, protocol.progress_file(args.spawn)),
                           os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND, 0o644)
    if "steps" in spec:
        child.run_steps(spec["steps"])
    else:
        child.auto(spec.get("auto", {"pages": 1}))
    os._exit(spec.get("exit_code", 0))


main()
'''

REAL_PROTOCOL = Path(protocol.__file__)
REAL_CHILD = runner.default_repo_root() / "app" / "sandbox" / "__main__.py"
MB = 1024 * 1024


@pytest.fixture(autouse=True)
def _fast_ticks(monkeypatch):
    # The 0.5 s tick is the production cadence; a shorter one only makes the
    # timing assertions below tighter and the file faster. Nothing here asserts
    # on the tick itself.
    monkeypatch.setattr(runner, "TICK_SECONDS", 0.1)


def _fake_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "app" / "sandbox").mkdir(parents=True)
    (repo / "app" / "__init__.py").write_text("")
    (repo / "app" / "sandbox" / "__init__.py").write_text("")
    shutil.copy(REAL_PROTOCOL, repo / "app" / "sandbox" / "protocol.py")
    (repo / "app" / "sandbox" / "__main__.py").write_text(FAKE_CHILD)
    return repo


def _limits(**over) -> SandboxLimits:
    base = dict(
        memory_bytes=256 * MB, cpu_seconds=60, max_output_file_bytes=8 * MB,
        max_output_total_bytes=64 * MB, max_open_files=64, max_processes=1, max_pages=50,
        max_page_side_pt=14400, thumb_long_side=64, thumb_jpeg_quality=70, full_long_side=256,
        full_small_long_side=128, full_small_threshold_pt=1300, full_jpeg_quality=85,
        max_text_chars_per_page=1000, deadline_seconds=30, heartbeat_seconds=10,
    )
    base.update(over)
    return SandboxLimits(**base)


def _run(tmp_path: Path, script: dict, *, limits: SandboxLimits | None = None, **kw) -> SandboxResult:
    """Run the fake child on `script` with short budgets. Keyword overrides
    reach run_sandbox unchanged."""
    repo = _fake_repo(tmp_path)
    pdf = tmp_path / "input.pdf"
    pdf.write_text(json.dumps(script))
    defaults: dict = dict(
        limits=limits or _limits(), scratch_root=tmp_path / "scratch", open_timeout_seconds=5,
        page_stall_seconds=5, file_timeout_seconds=20, max_restarts=3, max_pages_remaining=100,
        uid_slot=None, should_abort=lambda: False, on_progress=lambda *a: None,
        renew=lambda: True, repo_root=repo, python_executable=sys.executable,
    )
    defaults.update(kw)
    return runner.run_sandbox(pdf, **defaults)


def _capture_popen(monkeypatch, *, strip_user: bool = False) -> list[tuple[list, dict]]:
    """Record every launch (argv, kwargs, and the parent-written inputs as they
    were at spawn time, since the scratch dir is gone by the time a test can
    look). `strip_user` drops the uid-switch kwargs so a non-root developer can
    rehearse the root path."""
    calls: list[tuple[list, dict]] = []
    real = subprocess.Popen

    def fake(argv, **kw):
        record = dict(kw)
        for flag in ("--limits", "--skip-file", "--list-file"):
            if flag in argv:
                record[flag] = Path(argv[argv.index(flag) + 1]).read_text()
        calls.append((list(argv), record))
        if strip_user:
            for key in ("user", "group", "extra_groups"):
                kw.pop(key, None)
        return real(argv, **kw)

    monkeypatch.setattr(runner, "_popen", fake)
    return calls


def _jpeg(w: int, h: int) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), (10, 20, 30)).save(buf, "JPEG", quality=70)
    return buf.getvalue()


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _auto(pages: int, **spec) -> dict:
    return {"auto": {"pages": pages, **spec}}


def _steps(*steps) -> dict:
    return {"steps": list(steps)}


def _pid_dead(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def _no_scratch_left(tmp_path: Path) -> bool:
    scratch = tmp_path / "scratch"
    return not scratch.exists() or not any(p.name.startswith("rfp-sandbox-") for p in scratch.iterdir())


# ── Limits and the self-test fixture ─────────────────────────────────────────


def test_limits_come_from_settings_and_the_hash_ignores_the_deadline():
    from types import SimpleNamespace

    settings = SimpleNamespace(
        rfp_ingest_sandbox_memory_mb=1536, rfp_ingest_sandbox_cpu_seconds=1800,
        rfp_ingest_sandbox_output_file_mb=64, rfp_ingest_sandbox_disk_mb=3072,
        rfp_ingest_max_pages_per_file=3000, rfp_ingest_max_page_side_pt=14400,
        rfp_ingest_thumb_long_side=1568, rfp_ingest_thumb_jpeg_quality=70,
        rfp_ingest_full_long_side=4000, rfp_ingest_full_small_long_side=2200,
        rfp_ingest_full_small_threshold_pt=1300, rfp_ingest_full_jpeg_quality=85,
        rfp_ingest_max_text_chars_per_page=200000, rfp_ingest_file_timeout_max_seconds=14400,
    )
    limits = SandboxLimits.from_settings(settings)
    doc = limits.to_dict()
    assert set(doc) == set(protocol.LIMIT_KEYS)
    assert protocol.validate_limits(doc) == doc
    assert doc["memory_bytes"] == 1536 * MB and doc["max_output_file_bytes"] == 64 * MB
    assert doc["max_open_files"] == 64 and doc["max_processes"] == 1
    assert doc["heartbeat_seconds"] == 10 and doc["deadline_seconds"] == 14400
    # The per-spawn deadline never changes the hash and never drops below 5 s.
    assert limits.with_deadline(7).limits_hash() == limits.limits_hash()
    assert limits.with_deadline(1).deadline_seconds == 5
    assert json.loads(limits.to_json()) == doc
    with pytest.raises(ValueError):
        _limits(thumb_long_side=999)  # thumb above full_small is a parent bug


def test_embedded_selftest_pdf_is_a_valid_one_page_document():
    from pypdf import PdfReader

    pdf = runner.embedded_selftest_pdf()
    assert pdf.startswith(b"%PDF-1.4") and pdf.rstrip().endswith(b"%%EOF")
    reader = PdfReader(io.BytesIO(pdf))
    assert len(reader.pages) == 1
    assert [float(v) for v in reader.pages[0].mediabox] == [0.0, 0.0, 612.0, 792.0]
    assert "self-test" in reader.pages[0].extract_text()
    # The xref offsets are computed, not typed: every entry points at "N 0 obj".
    xref_at = int(pdf.split(b"startxref\n")[1].split(b"\n")[0])
    assert pdf[xref_at:xref_at + 4] == b"xref"


# ── The happy path and the launch contract ──────────────────────────────────


def test_complete_run_returns_every_page_with_validated_bytes(tmp_path):
    script = _auto(3, overrides={"1": {"text": "line​ one\x00\n\tkeep", "hazards": {"uri_links": 2}}})
    res = _run(tmp_path, script | {"stderr": "warning: fake child noise\n"})
    assert res.status == "complete" and res.code is None and res.detail is None
    assert [p.index for p in res.pages] == [0, 1, 2]
    assert all(p.status == "ok" for p in res.pages)
    for page in res.pages:
        assert page.thumb[:2] == b"\xff\xd8" and page.full[:2] == b"\xff\xd8"
        assert page.thumb_meta["w"] == 64 and page.full_meta["w"] == 128
        assert page.tier == "full_small" and page.rotation == 0 and page.render_ms == 3
    # Text is re-sanitized by the parent: the zero-width space and NUL are gone,
    # the newline and tab survive, and the hazard is counted even though the
    # child reported none (merged by max).
    page1 = res.pages[1]
    assert page1.text == "line one\n\tkeep"
    assert page1.text_chars == len(page1.text)
    assert page1.text_hazards == {"format_chars": 1, "private_use": 0, "unassigned": 0,
                                  "replacement_chars": 0}
    assert res.hazards["uri_links"] == 2 and res.hazards["javascript_actions"] == 0
    assert res.spawns == 2 and res.restarts == 0 and res.bounds_hit == []
    assert res.start["skip_count"] == 0 and res.ready["platform"] == sys.platform
    assert res.document["page_count"] == 3 and res.document["metadata"]["title"] == "Fake title"
    assert res.end["aborted"] is None and res.peak_rss_kb == 4321
    assert "fake child noise" in res.stderr_tail
    assert res.uid is None and res.gid is None and res.slot is None
    assert res.elapsed_ms >= 0
    assert _no_scratch_left(tmp_path)


def test_page_text_stops_at_the_per_file_budget_instead_of_piling_up(tmp_path):
    # Four 400-byte pages against a 500-byte file budget: the budget is spent in
    # index order, so the pages that cross it are the last ones to carry text and
    # every later page comes back with none. Without this the parent would hold
    # max_pages_per_file x max_text_chars_per_page of page text at once.
    script = _auto(4, overrides={str(i): {"text": "abcd" * 100} for i in range(4)})
    res = _run(tmp_path, script, max_text_bytes_per_file=500)
    assert res.status == "complete"
    assert [p.text for p in res.pages] == ["abcd" * 100, "abcd" * 100, "", ""]
    # The character count is the true one on every page, the child's own
    # per-page truncation flag is untouched, and the budget is reported.
    assert [p.text_chars for p in res.pages] == [400, 400, 400, 400]
    assert all(p.text_truncated is False for p in res.pages)
    assert res.bounds_hit == ["text_bytes"]
    # A budget the file fits in changes nothing and records no bound.
    res = _run(tmp_path / "roomy", script, max_text_bytes_per_file=64 * 1024)
    assert [p.text for p in res.pages] == ["abcd" * 100] * 4
    assert res.bounds_hit == []


def _sink_run(tmp_path: Path, script: dict, sink, **kw) -> SandboxResult:
    return _run(tmp_path, script, page_sink=sink, **kw)


def test_page_sink_receives_every_ok_page_in_order_and_the_result_holds_no_bytes(tmp_path):
    seen: list[tuple[int, bytes, bytes]] = []
    res = _sink_run(tmp_path, _auto(3), lambda page, thumb, full: seen.append(
        (page.index, thumb, full)))
    assert res.status == "complete"
    assert [i for i, _t, _f in seen] == [0, 1, 2]
    for index, thumb, full in seen:
        page = res.pages[index]
        # What the sink got is byte-identical to what validation checked.
        assert hashlib.sha256(thumb).hexdigest() == page.thumb_meta["sha256"]
        assert hashlib.sha256(full).hexdigest() == page.full_meta["sha256"]
        assert len(thumb) == page.thumb_meta["bytes"] and thumb[:2] == b"\xff\xd8"
        assert len(full) == page.full_meta["bytes"] and full[:2] == b"\xff\xd8"
    # The runner kept nothing: no page carries image bytes out of the call.
    assert all(p.thumb is None and p.full is None for p in res.pages)
    assert [p.text for p in res.pages] == ["page 0 text\n", "page 1 text\n", "page 2 text\n"]
    assert _no_scratch_left(tmp_path)


def test_page_sink_sees_only_the_pages_that_survived_verify(tmp_path):
    seen: list[int] = []
    res = _sink_run(tmp_path, _auto(3) | {"verify": {"fail": ["full/0001.jpg"]}},
                    lambda page, thumb, full: seen.append(page.index))
    assert res.status == "complete"
    assert seen == [0, 2]
    assert (res.pages[1].status, res.pages[1].code) == ("failed", "verify")


@pytest.mark.parametrize("script,expected", [
    (_auto(10, fail=[0, 1, 2]), ("rejected", "too_many_failed_pages")),
    (_auto(2) | {"verify": {"raw": "garbage\n"}}, ("failed", "invalid_output")),
    (_auto(2) | {"verify": {"exit_before_log": protocol.EXIT_BAD_ARGS}}, ("failed", "spawn")),
    (_auto(2, crash_at=0, crash_how="kill") | {"spawns": [
        {"auto": {"pages": 2, "crash_at": 0, "crash_how": "kill"}},
        {"auto": {"pages": 2, "crash_at": 1, "crash_how": "kill"}},
        {"auto": {"pages": 2, "crash_at": 0, "crash_how": "kill"}},
    ]}, ("rejected", "crash_loop")),
])
def test_page_sink_is_never_called_for_a_result_that_is_not_complete(tmp_path, script, expected):
    seen: list[int] = []
    res = _sink_run(tmp_path, script, lambda page, thumb, full: seen.append(page.index),
                    max_restarts=1)
    assert (res.status, res.code) == expected
    assert seen == []


def test_page_sink_rechecks_the_digest_and_a_changed_artifact_fails_the_file(tmp_path, monkeypatch):
    # The window between validation and delivery is closed by re-hashing: a
    # page whose bytes changed underneath makes the whole file invalid_output
    # and the sink hears nothing more.
    calls = _capture_popen(monkeypatch)
    seen: list[int] = []

    def sink(page, thumb, full):
        seen.append(page.index)
        if page.index == 0:
            out = Path(calls[0][1]["cwd"])
            (out / "full" / "0001.jpg").write_bytes(b"swapped after validation")

    res = _sink_run(tmp_path, _auto(3), sink)
    assert (res.status, res.code) == ("failed", "invalid_output")
    assert res.pages == []
    assert seen == [0]
    assert _no_scratch_left(tmp_path)


def test_an_exception_from_the_page_sink_propagates_and_the_scratch_is_gone(tmp_path):
    def sink(page, thumb, full):
        if page.index == 1:
            raise RuntimeError("upload failed")

    with pytest.raises(RuntimeError, match="upload failed"):
        _sink_run(tmp_path, _auto(3), sink)
    assert _no_scratch_left(tmp_path)


def test_without_a_page_sink_the_validated_bytes_still_ride_out_on_the_pages(tmp_path):
    res = _run(tmp_path, _auto(2))
    assert res.status == "complete"
    assert all(p.thumb[:2] == b"\xff\xd8" and p.full[:2] == b"\xff\xd8" for p in res.pages)


def test_launch_uses_the_exact_protocol_command_line_and_environment(tmp_path, monkeypatch):
    calls = _capture_popen(monkeypatch)
    res = _run(tmp_path, _auto(1))
    assert res.status == "complete"
    (argv0, kw0), (argv1, kw1) = calls
    out = Path(kw0["cwd"])
    work = out.parent
    assert out.name == "out" and work.name.startswith("rfp-sandbox-")
    repo = tmp_path / "repo"
    assert argv0 == [
        sys.executable, "-I", "-X", "utf8", "-c", protocol.BOOTSTRAP, str(repo),
        "--spawn", "0", "--input", str(work / "source.pdf"), "--out", str(out),
        "--limits", str(work / "limits.00.json"),
    ]
    assert argv1 == [
        sys.executable, "-I", "-X", "utf8", "-c", protocol.BOOTSTRAP, str(repo),
        "--verify", "--out", str(out), "--limits", str(work / "limits.01.json"),
        "--list-file", str(work / "verify-list.txt"),
    ]
    for kw in (kw0, kw1):
        assert kw["cwd"] == str(out)
        assert kw["env"] == {"LANG": "C.UTF-8", "TMPDIR": str(out / "tmp"), "HOME": str(out / "tmp")}
        assert kw["stdin"] is subprocess.DEVNULL and kw["stdout"] is subprocess.DEVNULL
        assert isinstance(kw["stderr"], int) and kw["stderr"] > 2
        assert kw["close_fds"] is True and kw["start_new_session"] is True
        assert "user" not in kw and "group" not in kw
    limits0 = json.loads(kw0["--limits"])
    assert protocol.validate_limits(limits0)["deadline_seconds"] <= 20
    assert kw1["--list-file"] == "thumb/0000.jpg\nfull/0000.jpg\n"


def test_progress_is_parsed_incrementally_while_the_child_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "PROGRESS_INTERVAL_SECONDS", 0.2)
    seen: list[tuple[str, int, int | None]] = []
    script = _auto(4, page_delay=0.35) | {"verify": {"delay": 0.15}}
    res = _run(tmp_path, script, on_progress=lambda *a: seen.append(a))
    assert res.status == "complete"
    sandbox = [s for s in seen if s[0] == protocol.PHASE_SANDBOX]
    # pages_done climbs while the child is still running: complete events are
    # counted as they land, not when the child exits.
    assert len({s[1] for s in sandbox}) >= 3
    assert [s[1] for s in sandbox] == sorted(s[1] for s in sandbox)
    assert all(isinstance(s[2], int) for s in seen)
    assert any(s[0] == protocol.PHASE_VERIFY for s in seen)


def test_child_that_lingers_after_end_is_killed_without_changing_the_verdict(tmp_path):
    res = _run(tmp_path, _auto(1, linger=True), page_stall_seconds=1)
    assert res.status == "complete" and res.bounds_hit == []


# ── Torn lines ──────────────────────────────────────────────────────────────


def test_torn_trailing_line_is_discarded_when_the_child_crashed(tmp_path):
    res = _run(tmp_path, _auto(3, crash_at=1, torn=True, crash_how="segv", partial=True))
    assert res.status == "complete" and res.restarts == 1 and res.spawns == 3
    codes = {p.index: (p.status, p.code) for p in res.pages}
    assert codes == {0: ("ok", None), 1: ("failed", "crash"), 2: ("ok", None)}
    assert res.pages[1].detail == runner.PAGE_DETAIL_MESSAGES["crash"]
    assert res.pages[1].thumb is None


def test_torn_trailing_line_after_a_clean_exit_is_invalid_output(tmp_path):
    script = _steps({"start": {}}, {"ready": {}}, {"document": {"page_count": 1}},
                    {"page_ok": 0}, {"end": {"pages_ok": 1}}, {"raw": '{"event": "heartbeat"'},
                    {"exit": 0})
    res = _run(tmp_path, script)
    assert (res.status, res.code) == ("failed", "invalid_output")
    assert res.pages == []


# ── Every validation rejection ──────────────────────────────────────────────


def _ok_then(*extra_steps, page_count=1, pages=(0,)):
    steps = [{"start": {}}, {"ready": {}}, {"document": {"page_count": page_count}}]
    steps += [{"page_ok": i} for i in pages]
    steps += list(extra_steps)
    steps += [{"end": {"pages_ok": len(pages)}}]
    return {"steps": steps}


def _page_with(overrides: dict) -> dict:
    return _steps({"start": {}}, {"ready": {}}, {"document": {"page_count": 1}},
                  {"page_ok": 0, "overrides": overrides}, {"end": {"pages_ok": 1}})


_COM_JPEG = _jpeg(64, 32)
_COM_JPEG = _COM_JPEG[:2] + b"\xff\xfe\x00\x05abc" + _COM_JPEG[2:]   # a COM segment after SOI

INVALID_CASES = {
    "bad json": _ok_then({"raw": "{not json}\n"}),
    "nan number": _ok_then({"raw": '{"event": "heartbeat", "elapsed_ms": NaN}\n'}),
    # 60,001 bytes: under the 64 KB line cap (so it reaches _parse_line, unlike
    # "line too long") and well over the ~10k nesting levels json.loads takes.
    "deep nesting": _ok_then({"raw": "[" * 30000 + "]" * 30000 + "\n"}),
    "unknown event": _ok_then({"event": {"event": "bogus"}}),
    "event after end": _ok_then({"end": {"pages_ok": 1}}),
    "heartbeat before start": _steps({"heartbeat": {}}, {"start": {}}, {"ready": {}},
                                     {"document": {"page_count": 1}}, {"page_ok": 0},
                                     {"end": {}}),
    "page_count zero": _steps({"start": {}}, {"ready": {}}, {"document": {"page_count": 0}},
                              {"end": {}}),
    "page_count above the file cap": _steps({"start": {}}, {"ready": {}},
                                            {"document": {"page_count": 51}}, {"end": {}}),
    "page without page_start": _page_with({"no_start": True}),
    "wrong sha": _page_with({"thumb_meta": {"sha256": "0" * 64}}),
    "wrong size": _page_with({"full_meta": {"bytes": 17}}),
    "wrong dims": _page_with({"thumb_meta": {"w": 5}}),
    "dims above the tier": _page_with({"full_dims": [300, 10]}),
    "text sha mismatch": _page_with({"text_meta": {"sha256": "a" * 64}}),
    "missing text file": _page_with({"omit": ["text"]}),
    "unknown page hazard": _page_with({"hazards": {"evil": 1}}),
    "negative text hazard": _page_with({"text_hazards": {"format_chars": -1}}),
    "extra file at the top": _ok_then({"write": {"path": "notes.txt", "data": "x"}}),
    "extra file in a tier dir": _ok_then({"write": {"path": "thumb/extra.jpg", "kind": "jpeg",
                                                    "w": 4, "h": 4}}),
    "forged verify log": _ok_then({"write": {"path": "verify.jsonl", "data": ""}}),
    "symlinked file": _page_with({"omit": ["thumb"], "event": {}}) | {"symlink": True},
    "symlinked subdir": _ok_then({"write": {"path": "thumb2", "kind": "symlink",
                                            "target": "thumb"}}),
    "fifo": _ok_then({"write": {"path": "full/0001.jpg", "kind": "fifo"}}, page_count=2),
    "dotdot in a name": _page_with({"thumb_meta": {"file": "thumb/../0000.jpg"}}),
    "absolute name": _page_with({"full_meta": {"file": "/etc/passwd"}}),
    "non-jpeg bytes": _page_with({"thumb_b64": _b64(b"not a jpeg at all, honestly")}),
    "disallowed jpeg marker": _page_with({"thumb_b64": _b64(_COM_JPEG)}),
    "trailing bytes after eoi": _page_with({"thumb_b64": _b64(_jpeg(64, 32) + b"\x00")}),
    "index out of range": _steps({"start": {}}, {"ready": {}}, {"document": {"page_count": 2}},
                                 {"page_ok": 0}, {"page_ok": 5}, {"end": {}}),
    "duplicate index": _ok_then({"page_ok": 0}),
    "oversized text artifact": _page_with({"text": "x" * 5000}),
    # Inside the 4-bytes-per-char byte cap but over max_text_chars_per_page.
    "text longer than the character cap": _page_with({"text": "x" * 2000}),
    "zero page width": _page_with({"event": {"width_pt": 0.0}}),
    "zero page height": _page_with({"event": {"height_pt": 0.0}}),
    "oversized thumb artifact": _page_with({"thumb_meta": {"bytes": 1000}}) | {"sparse_thumb": True},
    "line too long": _ok_then({"raw": '{"event": "heartbeat", "pad": "' + "x" * 70000 + '"}\n'}),
    "too many lines": _steps({"start": {}}, {"ready": {}}, {"document": {"page_count": 1}},
                             {"heartbeats": 4300}, {"page_ok": 0}, {"end": {}}),
    "end while a page is open": _steps({"start": {}}, {"ready": {}},
                                       {"document": {"page_count": 1}}, {"page_start": 0},
                                       {"end": {}}),
    "child ended without covering every page": _steps({"start": {}}, {"ready": {}},
                                                      {"document": {"page_count": 2}},
                                                      {"page_ok": 0}, {"end": {}}),
}


def _materialize(case: dict) -> dict:
    """Two cases need a file written AFTER the page's own files: a symlink in
    place of the thumb, and a sparse thumb bigger than the parent's cap."""
    if case.pop("symlink", False):
        steps = case["steps"]
        steps.insert(4, {"write": {"path": "thumb/0000.jpg", "kind": "symlink",
                                   "target": "../full/0000.jpg"}})
    if case.pop("sparse_thumb", False):
        steps = case["steps"]
        steps.insert(4, {"write": {"path": "thumb/0000.jpg", "kind": "sparse",
                                   "size": protocol.MAX_THUMB_BYTES + 1}})
    return case


@pytest.mark.parametrize("name", sorted(INVALID_CASES))
def test_validation_rejection_is_invalid_output_with_no_bytes(tmp_path, name):
    case = _materialize(json.loads(json.dumps(INVALID_CASES[name])))
    res = _run(tmp_path, case, limits=_limits(max_output_total_bytes=64 * MB))
    assert (res.status, res.code) == ("failed", "invalid_output"), name
    assert res.detail == protocol.VERDICT_MESSAGES["invalid_output"]
    assert res.pages == [], name
    assert _no_scratch_left(tmp_path)


@pytest.mark.parametrize("line", [
    b"[1]",                                          # not an object
    b'"heartbeat"',                                  # not an object
    b'{"event": 1}',                                 # event is not a string
    b'{"event": "heartbeat", "x": Infinity}',        # parse_constant refuses it
    b'{"event": "heartbeat", "x": NaN}',
    b"\xff\xfe",                                     # not UTF-8
    b"{oops}",
])
def test_parse_line_refuses_everything_that_is_not_a_strict_event_object(line):
    with pytest.raises(Exception) as exc:
        runner._parse_line(line)
    assert type(exc.value).__name__ == "_Invalid"


def test_parse_line_accepts_a_plain_event_object():
    assert runner._parse_line(b'{"event": "heartbeat", "elapsed_ms": 1}')["event"] == "heartbeat"


def test_child_that_plants_the_next_spawns_stderr_file_is_only_its_own_fault(tmp_path):
    # <out> is the child's own directory, so it can create the name the parent
    # will derive for the respawn. That must fail THIS file (invalid_output),
    # never raise a spawn error that would abandon every other file in the run.
    script = {"spawns": [
        _steps({"start": {}}, {"ready": {}}, {"document": {"page_count": 2}},
               {"write": {"path": protocol.stderr_file(1), "data": "planted"}},
               {"page_start": 0}, {"crash": "exit"}),
        _auto(2),
    ]}
    res = _run(tmp_path, script)
    assert (res.status, res.code) == ("failed", "invalid_output")
    assert res.pages == [] and res.restarts == 1
    assert _no_scratch_left(tmp_path)


def test_page_with_a_child_failure_code_is_kept_as_a_placeholder(tmp_path):
    res = _run(tmp_path, _auto(2, fail=[1], detail="PDFium said ‮no\x07"))
    assert res.status == "complete"
    page = res.pages[1]
    assert (page.status, page.code) == ("failed", "render_error")
    assert page.detail == "PDFium said no"   # bidi override and BEL stripped
    assert page.thumb is None and page.text is None and page.text_chars == 0
    assert res.pages[0].status == "ok"


# ── Crash attribution ───────────────────────────────────────────────────────


@pytest.mark.parametrize("script", [
    {"exit_before_log": 1},                                    # no start at all
    _steps({"start": {}}, {"exit": 1}),                        # start without ready
    _steps({"start": {}}, {"ready": {}}, {"document": {"page_count": 1}},
           {"page_ok": 0}, {"end": {}}, {"exit": protocol.EXIT_BAD_ARGS}),
])
def test_missing_start_or_ready_or_bad_args_is_failed_spawn(tmp_path, script):
    res = _run(tmp_path, script)
    assert (res.status, res.code) == ("failed", "spawn")
    assert res.restarts == 0 and res.spawns == 1 and res.pages == []


def test_death_between_ready_and_document_is_rejected_unreadable(tmp_path):
    res = _run(tmp_path, _steps({"start": {}}, {"ready": {}}, {"exit": 1}))
    assert (res.status, res.code) == ("rejected", "unreadable")
    assert res.restarts == 0


def test_crash_mid_page_blames_that_page_and_respawns_with_a_skip_file(tmp_path, monkeypatch):
    calls = _capture_popen(monkeypatch)
    res = _run(tmp_path, _auto(3, crash_at=1, partial=True))
    assert res.status == "complete" and res.restarts == 1 and res.spawns == 3
    assert {p.index: p.code for p in res.pages} == {0: None, 1: "crash", 2: None}
    argv1, kw1 = calls[1]
    assert "--spawn" in argv1 and argv1[argv1.index("--spawn") + 1] == "1"
    # The skip list carries the blamed index AND the pages already done, so a
    # respawn never re-renders what the parent already holds.
    assert kw1["--skip-file"] == "0\n1\n"
    assert res.start["skip_count"] == 2
    deadline = json.loads(kw1["--limits"])["deadline_seconds"]
    assert 5 <= deadline <= 20


def test_crash_with_no_dangling_page_start_blames_the_first_uncovered_index(tmp_path):
    script = {"spawns": [
        _steps({"start": {}}, {"ready": {}}, {"document": {"page_count": 2}}, {"exit": 1}),
        _auto(2),
    ]}
    res = _run(tmp_path, script)
    assert res.status == "complete" and res.restarts == 1
    assert {p.index: p.code for p in res.pages} == {0: "crash", 1: None}


@pytest.mark.parametrize("memory_applied, how, expected", [
    (True, "kill", "memory"),
    (True, "abrt", "memory"),
    (True, "exit", "memory"),
    (True, "segv", "crash"),     # another signal is a crash, not a memory kill
    (False, "kill", "crash"),    # without RLIMIT_AS in force a SIGKILL proves nothing
])
def test_memory_attribution_needs_the_limit_and_a_kill_or_nonzero_exit(
    tmp_path, memory_applied, how, expected
):
    res = _run(tmp_path, _auto(2, crash_at=0, crash_how=how, memory_applied=memory_applied))
    assert res.status == "complete"
    assert res.pages[0].code == expected
    assert res.pages[0].detail == runner.PAGE_DETAIL_MESSAGES[expected]


def test_stall_kills_the_child_blames_the_page_as_stall_and_respawns(tmp_path):
    started = time.monotonic()
    res = _run(tmp_path, _auto(3, hang_at=1), page_stall_seconds=1)
    assert time.monotonic() - started < 6
    assert res.status == "complete" and res.restarts == 1
    assert res.pages[1].code == "stall" and "page_stall" in res.bounds_hit


def test_restart_cap_turns_a_repeat_crasher_into_crash_loop(tmp_path):
    res = _run(tmp_path, _auto(4, crash_first=True), max_restarts=1, restart_ratio=0.0)
    assert (res.status, res.code) == ("rejected", "crash_loop")
    assert res.restarts == 1 and "child_restarts" in res.bounds_hit
    # The failed placeholders known so far are still reported for the audit trail.
    assert [(p.index, p.code) for p in res.pages] == [(0, "crash"), (1, "crash")]


def test_restart_cap_scales_with_the_page_count_ratio(tmp_path):
    # 4 pages x 0.5 = 2 restarts allowed even though max_restarts is 0.
    res = _run(tmp_path, _auto(4, crash_first=True), max_restarts=0, restart_ratio=0.5)
    assert (res.status, res.code) == ("rejected", "crash_loop")
    assert res.restarts == 2


def test_crash_with_nothing_left_to_blame_is_crash_loop_not_a_respawn(tmp_path):
    script = _steps({"start": {}}, {"ready": {}}, {"document": {"page_count": 1}},
                    {"page_ok": 0}, {"exit": 1})
    res = _run(tmp_path, script)
    assert (res.status, res.code) == ("rejected", "crash_loop")
    assert res.restarts == 0 and res.spawns == 1


def test_respawn_must_repeat_the_pinned_page_count(tmp_path):
    res = _run(tmp_path, {"spawns": [_auto(2, crash_at=1), _auto(3)]})
    assert (res.status, res.code) == ("rejected", "unreadable")
    assert res.restarts == 1


def test_reject_on_the_first_spawn_keeps_the_child_code(tmp_path):
    script = _steps({"start": {}}, {"ready": {}},
                    {"reject": {"code": "encrypted", "detail": "needs​ a password"}})
    res = _run(tmp_path, script)
    assert (res.status, res.code) == ("rejected", "encrypted")
    assert res.detail == protocol.VERDICT_MESSAGES["encrypted"]
    assert res.document == {"event": "reject", "code": "encrypted", "detail": "needs a password"}


def test_reject_on_a_respawn_is_unreadable(tmp_path):
    second = _steps({"start": {}}, {"ready": {}}, {"reject": {"code": "encrypted"}})
    res = _run(tmp_path, {"spawns": [_auto(2, crash_at=1), second]})
    assert (res.status, res.code) == ("rejected", "unreadable")


def test_respawn_that_renders_a_skipped_index_is_invalid_output(tmp_path):
    second = _steps({"start": {}}, {"ready": {}}, {"document": {"page_count": 2}},
                    {"page_ok": 1}, {"end": {}})
    res = _run(tmp_path, {"spawns": [_auto(2, crash_at=1), second]})
    assert (res.status, res.code) == ("failed", "invalid_output")


def test_end_aborted_is_resource_limit_and_uncovered_pages_read_aborted(tmp_path):
    script = _steps({"start": {}}, {"ready": {}}, {"document": {"page_count": 3}},
                    {"page_ok": 0}, {"end": {"pages_ok": 1, "aborted": "deadline"}})
    res = _run(tmp_path, script)
    assert (res.status, res.code) == ("failed", "resource_limit")
    assert res.bounds_hit == ["child_deadline"]
    assert [(p.index, p.code) for p in res.pages] == [(1, "aborted"), (2, "aborted")]
    assert all(p.thumb is None for p in res.pages)


# ── Parent-side bounds ──────────────────────────────────────────────────────


def test_open_timeout_fires_after_ready_and_heartbeats_do_not_extend_it(tmp_path):
    script = _steps({"start": {}}, {"ready": {}}, {"heartbeat_loop": 0.2})
    started = time.monotonic()
    res = _run(tmp_path, script, open_timeout_seconds=1)
    assert time.monotonic() - started < 5
    assert (res.status, res.code) == ("failed", "resource_limit")
    assert res.bounds_hit == ["open_timeout"]


def test_child_that_never_reaches_ready_is_failed_spawn_after_the_open_timeout(tmp_path):
    res = _run(tmp_path, _steps({"start": {}}, {"hang": True}), open_timeout_seconds=1)
    assert (res.status, res.code) == ("failed", "spawn")
    assert res.bounds_hit == ["open_timeout"]


def test_file_wall_clock_kills_the_child(tmp_path):
    script = _steps({"start": {}}, {"ready": {}}, {"document": {"page_count": 1}},
                    {"page_start": 0}, {"hang": True})
    res = _run(tmp_path, script, file_timeout_seconds=1, page_stall_seconds=10,
               open_timeout_seconds=10)
    assert (res.status, res.code) == ("failed", "resource_limit")
    assert res.bounds_hit == ["file_timeout"]


def test_post_exit_drain_reads_the_whole_log_not_one_window(tmp_path, monkeypatch):
    # One read() stops at the per-tick byte window. A child that pads its log
    # past that window before writing `end` would otherwise have its terminal
    # event dropped, and an innocent page blamed for a crash that never was.
    monkeypatch.setattr(runner, "_READ_CHUNK", 64)
    monkeypatch.setattr(runner, "_MAX_TAIL_BYTES_PER_TICK", 128)
    script = _steps({"start": {}}, {"ready": {}}, {"document": {"page_count": 2}},
                    {"page_ok": 0}, {"page_ok": 1}, {"heartbeats": 200},
                    {"end": {"pages_ok": 2}})
    res = _run(tmp_path, script)
    assert res.status == "complete" and res.restarts == 0
    assert [p.status for p in res.pages] == ["ok", "ok"]
    assert res.end["pages_ok"] == 2


def test_disk_quota_is_mirrored_by_the_parent_over_the_whole_out_dir(tmp_path):
    # A sparse 2 MB file inside tmp/ (never read, but always counted) trips the
    # 1 MB quota while the child idles.
    script = _steps({"start": {}}, {"ready": {}}, {"document": {"page_count": 1}},
                    {"write": {"path": "tmp/big.bin", "kind": "sparse", "size": 2 * MB}},
                    {"hang": True})
    limits = _limits(max_output_file_bytes=MB // 2, max_output_total_bytes=MB)
    res = _run(tmp_path, script, limits=limits)
    assert (res.status, res.code) == ("failed", "resource_limit")
    assert res.bounds_hit == ["disk_quota"]


def test_out_dir_entry_count_overflow_counts_as_a_quota_hit(tmp_path):
    # Thousands of ZERO-byte files: the byte sum stays near nothing, so the
    # entry cap (3 per page plus the slack) is the only thing that can stop a
    # child from exhausting the filesystem's inodes.
    script = _steps({"start": {}}, {"ready": {}}, {"document": {"page_count": 1}},
                    {"many": {"dir": "tmp", "count": 9000}}, {"hang": True})
    limits = _limits(max_pages=1, max_output_total_bytes=64 * MB)
    res = _run(tmp_path, script, limits=limits)
    assert (res.status, res.code) == ("failed", "resource_limit")
    assert res.bounds_hit == ["disk_quota"]
    assert _no_scratch_left(tmp_path)


def test_run_page_budget_kills_after_document_without_counting_a_restart(tmp_path):
    res = _run(tmp_path, _auto(5, page_delay=0.2), max_pages_remaining=3)
    assert (res.status, res.code) == ("rejected", "run_page_budget")
    assert res.bounds_hit == ["run_page_budget"] and res.restarts == 0
    assert res.document["page_count"] == 5


def test_cancel_mid_child_kills_it_and_returns_interrupted(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "PROGRESS_INTERVAL_SECONDS", 0.1)
    pids: list[int] = []
    calls = {"n": 0}

    def abort():
        calls["n"] += 1
        return calls["n"] > 3

    res = _run(tmp_path, _auto(20, page_delay=0.2), should_abort=abort,
               on_progress=lambda phase, done, pid: pids.append(pid))
    assert (res.status, res.code) == ("failed", "interrupted")
    assert res.detail == protocol.VERDICT_MESSAGES["interrupted"]
    assert pids and _pid_dead(pids[0])
    assert _no_scratch_left(tmp_path)


def test_lease_lost_mid_child_kills_it_and_returns_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "RENEW_INTERVAL_SECONDS", 0.3)
    monkeypatch.setattr(runner, "PROGRESS_INTERVAL_SECONDS", 0.1)
    pids: list[int] = []
    renews: list[float] = []

    def renew():
        renews.append(time.monotonic())
        return len(renews) < 2

    with pytest.raises(LeaseLost):
        _run(tmp_path, _auto(20, page_delay=0.2), renew=renew,
             on_progress=lambda phase, done, pid: pids.append(pid))
    assert len(renews) == 2
    assert pids and _pid_dead(pids[0])
    assert _no_scratch_left(tmp_path)


def test_stderr_tail_keeps_the_process_spawn_and_the_verify_spawn(tmp_path):
    # The verify child writes a preamble line on every run, so a rule that took
    # only the highest-numbered spawn that wrote anything would drop the
    # process spawn's traceback from every complete file.
    noisy = "Traceback (most recent call last):\n  boom ‮ hidden\n"
    res = _run(tmp_path, _auto(1, stderr=noisy) | {"stderr": noisy})
    assert res.status == "complete"
    assert res.stderr_tail == (
        "spawn 0:\nTraceback (most recent call last):\nboom hidden\n"
        "verify:\nsandbox verify: limits applied core=1,cpu=1"
    )


def test_stderr_tail_keeps_a_crashed_spawns_traceback_across_a_respawn(tmp_path):
    noisy = "Traceback (most recent call last):\n  ValueError: pdfium\n"
    script = {"spawns": [
        _auto(2, crash_at=0) | {"stderr": noisy},
        _auto(2),
    ]}
    res = _run(tmp_path, script)
    assert res.status == "complete" and res.restarts == 1 and res.spawns == 3
    # The respawn wrote nothing, so the tail walks back to spawn 0.
    assert res.stderr_tail.startswith("spawn 0:\nTraceback")
    assert "ValueError: pdfium" in res.stderr_tail
    assert "verify:\nsandbox verify" in res.stderr_tail


def test_stderr_tail_refuses_a_hardlinked_or_foreign_stderr_file(tmp_path):
    # The child owns <out>, so it can unlink the parent-created stderr file and
    # put a hard link to some other file in its place. Every parent read of a
    # child-reachable path checks regular + one link + our ownership.
    out = tmp_path / "out"
    out.mkdir()
    secret = tmp_path / "secret.env"
    secret.write_text("SUPABASE_SERVICE_ROLE_KEY=super-secret\n")
    name = out / protocol.stderr_file(0)
    name.write_text("honest child noise\n")
    out_fd = os.open(out, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert runner._read_stderr_tail(out_fd, 0) == "honest child noise"
        name.unlink()
        os.link(secret, name)
        assert runner._read_stderr_tail(out_fd, 0) == ""
        name.unlink()
        os.symlink(secret, name)
        assert runner._read_stderr_tail(out_fd, 0) == ""
    finally:
        os.close(out_fd)


def test_spawn_failure_is_a_parent_error_not_a_verdict(tmp_path):
    with pytest.raises(SandboxSpawnError):
        _run(tmp_path, _auto(1), python_executable=str(tmp_path / "no-such-python"))
    assert _no_scratch_left(tmp_path)


# ── Verify spawn and the gap rule ───────────────────────────────────────────


@pytest.mark.parametrize("verify", [
    {"fail": ["full/0001.jpg"]},
    {"omit": ["thumb/0001.jpg"]},
    {"dims": {"full/0001.jpg": [1, 1]}},
])
def test_verify_failure_marks_the_page_failed_verify_and_drops_its_images(tmp_path, verify):
    res = _run(tmp_path, _auto(2) | {"verify": verify})
    assert res.status == "complete"
    ok, bad = res.pages
    assert ok.status == "ok" and ok.thumb and ok.full
    assert (bad.status, bad.code) == ("failed", "verify")
    assert bad.thumb is None and bad.full is None
    assert bad.text == "page 1 text\n" and bad.thumb_meta["w"] == 64   # metadata kept for audit
    assert bad.detail == runner.PAGE_DETAIL_MESSAGES["verify"]
    assert res.spawns == 2


def test_verify_child_that_cannot_start_is_failed_spawn(tmp_path):
    res = _run(tmp_path, _auto(2, fail=[1])
               | {"verify": {"exit_before_log": protocol.EXIT_BAD_ARGS}})
    assert (res.status, res.code) == ("failed", "spawn")
    # Only the failed placeholder survives: an ok page whose bytes the caller
    # cannot have must not be counted as verified anywhere.
    assert [(p.index, p.status) for p in res.pages] == [(1, "failed")]
    assert all(p.thumb is None and p.full is None for p in res.pages)


@pytest.mark.parametrize("verify", [
    {"raw": "garbage\n"},
    {"extra": ["thumb/0007.jpg"]},
])
def test_malformed_verify_log_is_invalid_output(tmp_path, verify):
    res = _run(tmp_path, _auto(1) | {"verify": verify})
    assert (res.status, res.code) == ("failed", "invalid_output")
    assert res.pages == []


def test_verify_stall_fails_the_unconfirmed_pages_only(tmp_path):
    res = _run(tmp_path, _auto(2) | {"verify": {"hang": True, "omit": ["full/0001.jpg"]}},
               page_stall_seconds=1)
    assert res.status == "complete" and "page_stall" in res.bounds_hit
    assert res.pages[0].status == "ok" and res.pages[1].code == "verify"


def test_file_wall_clock_expiring_during_verify_does_not_discard_the_file(tmp_path):
    # Every page rendered and passed byte validation; only the decode pass is
    # still running when the file's wall clock runs out. Verify gets its own
    # budget, so the file still completes with its images.
    res = _run(tmp_path, _auto(2) | {"verify": {"hang": True}},
               file_timeout_seconds=1, page_stall_seconds=1, open_timeout_seconds=10)
    assert res.status == "complete" and res.code is None
    assert [p.status for p in res.pages] == ["ok", "ok"]
    assert all(p.thumb and p.full for p in res.pages)
    # The file clock is not what bounded the pass: verify had its own.
    assert res.bounds_hit == []


def test_a_verify_pass_cut_short_fails_only_the_pages_it_never_confirmed(tmp_path):
    res = _run(tmp_path, _auto(2) | {"verify": {"hang": True, "omit": ["full/0001.jpg"]}},
               file_timeout_seconds=1, page_stall_seconds=1, open_timeout_seconds=10)
    assert res.status == "complete"
    assert res.pages[0].status == "ok" and res.pages[0].thumb
    assert (res.pages[1].status, res.pages[1].code) == ("failed", "verify")
    assert res.bounds_hit == ["page_stall"]


def test_verify_running_out_of_its_own_budget_is_a_per_page_verdict(tmp_path):
    # Even when the decode pass itself runs long, the file is not thrown away:
    # the pages verify confirmed keep their images and the rest read verify.
    res = _run(tmp_path, _auto(10) | {"verify": {"delay": 0.2}},
               file_timeout_seconds=3, page_stall_seconds=1, open_timeout_seconds=10,
               min_failed_pages_allowed=10)
    assert res.status == "complete" and res.bounds_hit == ["file_timeout"]
    confirmed = [p for p in res.pages if p.status == "ok"]
    unconfirmed = [p for p in res.pages if p.code == "verify"]
    assert confirmed and unconfirmed and len(confirmed) + len(unconfirmed) == 10
    assert all(p.thumb and p.full for p in confirmed)
    assert all(p.thumb is None and p.text for p in unconfirmed)


def test_gap_rule_rejects_when_failed_pages_exceed_the_floor_and_ratio(tmp_path):
    res = _run(tmp_path, _auto(10, fail=[0, 1, 2]))
    assert (res.status, res.code) == ("rejected", "too_many_failed_pages")
    assert res.bounds_hit == ["failed_page_ratio"]
    assert len(res.pages) == 10 and all(p.thumb is None and p.full is None for p in res.pages)
    # Raising the floor to 3 makes the same file usable with gaps.
    res = _run(tmp_path / "again", _auto(10, fail=[0, 1, 2]), min_failed_pages_allowed=3)
    assert res.status == "complete"
    assert sum(1 for p in res.pages if p.status == "failed") == 3
    assert all(p.thumb for p in res.pages if p.status == "ok")


@pytest.mark.parametrize("pages", [1, 2])
def test_gap_rule_rejects_a_file_with_no_ok_page_even_inside_the_floor(tmp_path, pages):
    # The absolute floor (2) is the whole document here, so the ratio rule alone
    # would settle a file with nothing rendered as complete, and the service
    # would call it verified-with-gaps with an empty images PDF.
    res = _run(tmp_path, _auto(pages, fail=list(range(pages))))
    assert (res.status, res.code) == ("rejected", "too_many_failed_pages")
    assert res.bounds_hit == ["failed_page_ratio"]
    assert len(res.pages) == pages
    assert all(p.status == "failed" and p.thumb is None and p.full is None for p in res.pages)
    # One rendered page is enough to keep the floor's benefit.
    res = _run(tmp_path / "one_ok", _auto(pages + 1, fail=list(range(pages))))
    assert res.status == "complete"
    assert sum(1 for p in res.pages if p.status == "ok") == 1


# ── uid slots ───────────────────────────────────────────────────────────────


def test_no_uid_switch_when_the_parent_is_not_root_or_opted_out(tmp_path, monkeypatch):
    assert runner.acquire_uid_slot(tmp_path, sandbox_uid=65534, pool_base=60100, pool_size=4,
                                   renew=lambda: True, should_abort=lambda: False) is None
    monkeypatch.setattr(runner, "_geteuid", lambda: 0)
    assert runner.acquire_uid_slot(tmp_path, sandbox_uid=0, pool_base=60100, pool_size=4,
                                   renew=lambda: True, should_abort=lambda: False) is None


def test_uid_slots_are_claimed_in_order_shown_busy_and_released(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_geteuid", lambda: 0)
    monkeypatch.setattr(runner, "SLOT_POLL_SECONDS", 0.05)
    common = dict(sandbox_uid=65534, pool_base=60100, pool_size=2,
                  renew=lambda: True, should_abort=lambda: False)
    a = runner.acquire_uid_slot(tmp_path, **common)
    b = runner.acquire_uid_slot(tmp_path, **common)
    assert (a.uid, a.gid, a.slot) == (60100, 60100, 0)
    assert (b.uid, b.gid, b.slot) == (60101, 60101, 1)
    assert runner.slot_occupancy(tmp_path, pool_base=60100, pool_size=3) == [
        {"slot": 0, "uid": 60100, "busy": True},
        {"slot": 1, "uid": 60101, "busy": True},
        {"slot": 2, "uid": 60102, "busy": False},
    ]
    # Every slot busy: an abort while waiting is a spawn error, a lost lease is
    # LeaseLost, and neither leaks a lock.
    with pytest.raises(SandboxSpawnError):
        runner.acquire_uid_slot(tmp_path, **{**common, "should_abort": lambda: True})
    monkeypatch.setattr(runner, "RENEW_INTERVAL_SECONDS", 0.0)
    with pytest.raises(LeaseLost):
        runner.acquire_uid_slot(tmp_path, **{**common, "renew": lambda: False})
    a.release()
    a.release()   # idempotent
    assert runner.slot_occupancy(tmp_path, pool_base=60100, pool_size=2)[0]["busy"] is False
    c = runner.acquire_uid_slot(tmp_path, **common)
    assert c.slot == 0
    b.release()
    c.release()
    assert all(not s["busy"] for s in runner.slot_occupancy(tmp_path, pool_base=60100, pool_size=2))


def test_uid_slot_wait_is_bounded_so_a_wedged_pool_cannot_park_a_worker(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_geteuid", lambda: 0)
    monkeypatch.setattr(runner, "SLOT_POLL_SECONDS", 0.05)
    renews: list[int] = []
    common = dict(sandbox_uid=65534, pool_base=60100, pool_size=1,
                  renew=lambda: renews.append(1) or True, should_abort=lambda: False)
    held = runner.acquire_uid_slot(tmp_path, **common)
    assert held is not None
    started = time.monotonic()
    with pytest.raises(runner.SlotWaitTimeout):
        runner.acquire_uid_slot(tmp_path, **common, wait_timeout_seconds=0.3)
    assert time.monotonic() - started < 5
    # Nothing was claimed and nothing leaked: the holder still owns the slot.
    assert runner.slot_occupancy(tmp_path, pool_base=60100, pool_size=1) == [
        {"slot": 0, "uid": 60100, "busy": True},
    ]
    held.release()
    assert runner.acquire_uid_slot(tmp_path, **common).slot == 0


def test_uid_switch_reaches_popen_and_chown_and_ownership_is_enforced(tmp_path, monkeypatch):
    chowns: list[tuple[str, int, int]] = []
    monkeypatch.setattr(runner, "_chown", lambda p, u, g: chowns.append((Path(p).name, u, g)))
    calls = _capture_popen(monkeypatch, strip_user=True)
    slot = UidSlot(uid=os.getuid(), gid=os.getgid(), slot=2)
    res = _run(tmp_path, _auto(1), uid_slot=slot)
    assert res.status == "complete"
    assert (res.uid, res.gid, res.slot) == (os.getuid(), os.getgid(), 2)
    assert sorted(chowns) == [("out", os.getuid(), os.getgid()), ("tmp", os.getuid(), os.getgid())]
    assert len(calls) == 2
    for _, kw in calls:
        assert kw["user"] == os.getuid() and kw["group"] == os.getgid()
        assert kw["extra_groups"] == []
    # A file not owned by the slot uid is refused before it is read.
    foreign = UidSlot(uid=os.getuid() + 12345, gid=os.getgid(), slot=3)
    res = _run(tmp_path / "again", _auto(1), uid_slot=foreign)
    assert (res.status, res.code) == ("failed", "invalid_output")
    assert res.pages == [] and res.uid == os.getuid() + 12345


# ── Cleanup ─────────────────────────────────────────────────────────────────


def test_cleanup_scratch_removes_unreadable_trees_and_tolerates_absence(tmp_path):
    tree = tmp_path / "rfp-sandbox-x"
    (tree / "out" / "thumb").mkdir(parents=True)
    (tree / "out" / "thumb" / "0000.jpg").write_bytes(b"x")
    (tree / "out" / "tmp").mkdir()
    (tree / "out" / "tmp" / "deep").mkdir()
    (tree / "out" / "tmp" / "deep" / "f").write_text("y")
    os.chmod(tree / "out" / "tmp" / "deep", 0o000)
    os.chmod(tree / "out" / "tmp", 0o500)
    runner.cleanup_scratch(tree)
    assert not tree.exists()
    runner.cleanup_scratch(tree)   # already gone: no error
    link = tmp_path / "link"
    os.symlink(tmp_path / "elsewhere", link)
    runner.cleanup_scratch(link)
    assert not os.path.lexists(link)


def test_every_run_removes_its_scratch_directory(tmp_path):
    for i, script in enumerate([_auto(1), _steps({"exit": 1}), _auto(2, crash_at=0)]):
        res = _run(tmp_path / str(i), script)
        assert res.status in ("complete", "failed", "rejected")
        assert _no_scratch_left(tmp_path / str(i))


# ── The real child ──────────────────────────────────────────────────────────


def _real_run(tmp_path: Path, pdf_bytes: bytes, **kw) -> SandboxResult:
    pdf = tmp_path / "input.pdf"
    pdf.write_bytes(pdf_bytes)
    defaults: dict = dict(
        limits=_limits(thumb_long_side=200, full_small_long_side=300, full_long_side=400),
        scratch_root=tmp_path / "scratch", open_timeout_seconds=60, page_stall_seconds=60,
        file_timeout_seconds=180, max_restarts=2, max_pages_remaining=10, uid_slot=None,
        should_abort=lambda: False, on_progress=lambda *a: None, renew=lambda: True,
    )
    defaults.update(kw)
    return runner.run_sandbox(pdf, **defaults)


@pytest.mark.skipif(not REAL_CHILD.exists(), reason="real sandbox child not present")
@pytest.mark.parametrize("case", ["encrypted", "truncated", "zero pages", "too many pages"])
def test_real_child_reject_verdicts_reach_the_parent_unchanged(tmp_path, case):
    # The parent's event shapes are hand-written in this file; only pairing it
    # with the REAL child proves the reject paths still agree.
    from tests.test_rfp_sandbox_child import _blank_pdf, _encrypted_pdf, _zero_pages_pdf

    payload, limits, expected = {
        "encrypted": (_encrypted_pdf(), None, "encrypted"),
        "truncated": (_blank_pdf(2)[:200], None, "unreadable"),
        "zero pages": (_zero_pages_pdf(), None, "no_pages"),
        "too many pages": (_blank_pdf(3), _limits(max_pages=2), "too_many_pages"),
    }[case]
    kw = {"limits": limits} if limits is not None else {}
    res = _real_run(tmp_path, payload, **kw)
    assert (res.status, res.code) == ("rejected", expected), res.stderr_tail
    assert res.detail == protocol.VERDICT_MESSAGES[expected]
    assert res.pages == [] and res.spawns == 1
    assert res.document["event"] == "reject" and res.document["code"] == expected
    assert _no_scratch_left(tmp_path)


@pytest.mark.skipif(not REAL_CHILD.exists(), reason="real sandbox child not present")
def test_real_child_page_failure_pairs_with_the_parent_and_the_file_survives(tmp_path):
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=20000, height=20000)   # over max_page_side_pt
    writer.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    writer.write(buf)
    res = _real_run(tmp_path, buf.getvalue(), min_failed_pages_allowed=1)
    assert res.status == "complete", (res.code, res.stderr_tail)
    assert [(p.index, p.status, p.code) for p in res.pages] == [
        (0, "failed", "page_size"), (1, "ok", None),
    ]
    assert res.pages[0].detail and res.pages[1].thumb[:2] == b"\xff\xd8"


@pytest.mark.skipif(not REAL_CHILD.exists(), reason="real sandbox child not present")
def test_real_child_pages_reach_a_page_sink_instead_of_the_result(tmp_path):
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    writer.append(PdfReader(io.BytesIO(runner.embedded_selftest_pdf())))
    buf = io.BytesIO()
    writer.write(buf)
    seen: list[tuple[int, int, int]] = []
    res = _real_run(tmp_path, buf.getvalue(),
                    page_sink=lambda page, thumb, full: seen.append(
                        (page.index, len(thumb), len(full))))
    assert res.status == "complete", (res.code, res.stderr_tail)
    assert [i for i, _t, _f in seen] == [0]
    assert seen[0][1] == res.pages[0].thumb_meta["bytes"]
    assert seen[0][2] == res.pages[0].full_meta["bytes"]
    assert res.pages[0].thumb is None and res.pages[0].full is None
    assert "self-test" in res.pages[0].text


@pytest.mark.skipif(not REAL_CHILD.exists(), reason="real sandbox child not present")
def test_real_child_renders_a_two_page_pdf_end_to_end(tmp_path):
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    writer.append(PdfReader(io.BytesIO(runner.embedded_selftest_pdf())))
    writer.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    writer.write(buf)
    pdf = tmp_path / "two.pdf"
    pdf.write_bytes(buf.getvalue())
    limits = _limits(thumb_long_side=200, full_small_long_side=300, full_long_side=400)
    res = runner.run_sandbox(
        pdf, limits=limits, scratch_root=tmp_path / "scratch", open_timeout_seconds=60,
        page_stall_seconds=60, file_timeout_seconds=180, max_restarts=2,
        max_pages_remaining=10, uid_slot=None, should_abort=lambda: False,
        on_progress=lambda *a: None, renew=lambda: True,
    )
    assert res.status == "complete", (res.code, res.stderr_tail, res.bounds_hit)
    assert [p.index for p in res.pages] == [0, 1]
    for page in res.pages:
        assert page.status == "ok", (page.code, page.detail)
        assert page.thumb[:2] == b"\xff\xd8" and page.full[:2] == b"\xff\xd8"
        assert max(page.thumb_meta["w"], page.thumb_meta["h"]) == 200
        assert isinstance(page.text, str)
    assert "self-test" in res.pages[0].text
    assert res.start["limits_applied"] and res.ready["versions"]["pypdfium2"]
    assert res.spawns == 2 and res.restarts == 0
    assert _no_scratch_left(tmp_path)
