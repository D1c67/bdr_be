"""Smoke-run the RFP Ingestion Sandbox over a directory of real PDFs, no database.

Host-only tooling for the integrator: it drives `rfp_sandbox_runner.run_sandbox`
(the real child, the real limits document built from `Settings`, the default
timeouts, a scratch root under the OS temp dir) over every PDF found under a
directory, building the thumb-tier images PDF parts with
`rfp_image_pdf.ImagesPdfWriter` from the `page_sink` the runner calls per page
(so this tool holds one page of image bytes at a time, exactly as the service
does) and reading the parts back with pypdf.
Nothing touches Supabase, the queue or the network: this is the sandbox
boundary exercised end to end on files that were not written by a test.

Per file it prints: status/code, page count, pages ok/failed, restarts,
elapsed, thumb and full byte totals, bounds hit, peak RSS, and the sanitized
stderr tail when the child wrote anything. Then, per complete file, each
images part's size and page count. A `failed/invalid_output` or `failed/spawn`
row means a bug in the child, the sanitizer or the runner (the point of the
run); `rejected/encrypted` or `verified_with_gaps` on a real file is a
legitimate verdict worth a look. The exit code is 1 when any of the two bug
verdicts appeared, so the script can gate a release check.

Usage:
    cd bdr_be
    uv run python scripts/rfp_sandbox_smoke.py [DIR] [--limit N] [--keep]

DIR defaults to ../subs (the vendor spec sheets next to the repo). --limit
stops after N files (sorted by name). --keep leaves the scratch root and the
images parts on disk and prints where they are.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import time
from pathlib import Path

# Allow running as a plain script: put the project root (bdr_be) on the import
# path so `app` resolves.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import Settings  # noqa: E402
from app.sandbox import protocol  # noqa: E402
from app.services import rfp_sandbox_runner as runner  # noqa: E402
from app.services.rfp_image_pdf import ImagesPdfWriter  # noqa: E402

_DEFAULT_DIR = Path(__file__).resolve().parent.parent.parent / "subs"
_NAME_WIDTH = 44
_BUG_VERDICTS = {
    (runner.STATUS_FAILED, protocol.FAIL_INVALID_OUTPUT),
    (runner.STATUS_FAILED, protocol.FAIL_SPAWN),
}


def _find_pdfs(root: Path) -> list[Path]:
    """Every regular file under `root` whose bytes start with %PDF- or whose
    name ends in .pdf (case-insensitive); anything else is skipped."""
    found: list[Path] = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        if path.suffix.lower() == ".pdf":
            found.append(path)
            continue
        try:
            with path.open("rb") as fh:
                head = fh.read(1024)
        except OSError:
            continue
        if b"%PDF-" in head:
            found.append(path)
    return found


def _file_timeout_seconds(s: Settings) -> int:
    """clamp(base + per_page_ms x max_pages_per_file, 600, max), as the runner
    service computes it (rfp_ingest._file_timeout_seconds)."""
    raw = s.rfp_ingest_file_timeout_base_seconds + (
        s.rfp_ingest_file_timeout_per_page_ms * s.rfp_ingest_max_pages_per_file
    ) / 1000
    return int(max(600, min(raw, s.rfp_ingest_file_timeout_max_seconds)))


def _short(name: str, width: int = _NAME_WIDTH) -> str:
    return name if len(name) <= width else name[: width - 3] + "..."


def _fmt_bytes(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.1f}M"
    if n >= 1024:
        return f"{n / 1024:.0f}K"
    return str(n)


def _pypdf_page_count(path: Path) -> int | None:
    try:
        from pypdf import PdfReader
    except ImportError:
        return None
    return len(PdfReader(str(path), strict=True).pages)


def _run_one(pdf: Path, *, s: Settings, limits: runner.SandboxLimits, scratch_root: Path,
             page_sink):
    # run_sandbox hard-links its source copy and chmods it 0o644, which would
    # reach the caller's inode; hand it a private copy the way the service's
    # materialize step does, so the user's files are never touched.
    staged_dir = Path(tempfile.mkdtemp(prefix="src-", dir=scratch_root))
    staged = staged_dir / "source.pdf"
    shutil.copyfile(pdf, staged)
    slot = runner.acquire_uid_slot(
        scratch_root,
        sandbox_uid=s.rfp_ingest_sandbox_uid,
        pool_base=s.rfp_ingest_sandbox_uid_pool_base,
        pool_size=s.rfp_ingest_sandbox_uid_pool_size,
        renew=lambda: True,
        should_abort=lambda: False,
    )
    try:
        return runner.run_sandbox(
            staged,
            page_sink=page_sink,
            limits=limits,
            scratch_root=scratch_root,
            open_timeout_seconds=s.rfp_ingest_open_timeout_seconds,
            page_stall_seconds=s.rfp_ingest_page_stall_seconds,
            file_timeout_seconds=_file_timeout_seconds(s),
            max_restarts=s.rfp_ingest_max_child_restarts,
            max_pages_remaining=s.rfp_ingest_max_pages_per_run,
            uid_slot=slot,
            should_abort=lambda: False,
            on_progress=lambda phase, pages_done, pid: None,
            renew=lambda: True,
            restart_ratio=s.rfp_ingest_max_failed_page_ratio,
            min_failed_pages_allowed=s.rfp_ingest_min_failed_pages_allowed,
            failed_page_ratio=s.rfp_ingest_max_failed_page_ratio,
            max_text_bytes_per_file=s.rfp_ingest_max_text_bytes_per_file,
        )
    finally:
        if slot is not None:
            slot.release()
        shutil.rmtree(staged_dir, ignore_errors=True)


class _ImagesSink:
    """The `page_sink` handed to `run_sandbox`: every ok page of a complete
    file arrives here once, in index order, and goes straight into the open
    images part. A writer failure is recorded rather than raised, so one bad
    page still leaves the file's verdict row printed (it is reported as a bug
    like any other)."""

    def __init__(self, writer: ImagesPdfWriter) -> None:
        self.writer = writer
        self.pages = 0
        self.error: Exception | None = None

    def __call__(self, page, thumb: bytes, full: bytes) -> None:
        if self.error is not None:
            return
        try:
            self.writer.add_page(
                thumb,
                width_px=int(page.thumb_meta["w"]),
                height_px=int(page.thumb_meta["h"]),
                width_pt=float(page.width_pt),
                height_pt=float(page.height_pt),
            )
        except Exception as exc:  # noqa: BLE001 - reported, the run goes on
            self.error = exc
            return
        self.pages += 1


def _parts_of(writer: ImagesPdfWriter) -> list[tuple[Path, int, int | None]]:
    return [(part, part.stat().st_size, _pypdf_page_count(part)) for part in writer.close()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("dir", nargs="?", default=str(_DEFAULT_DIR))
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args(argv)

    root = Path(args.dir).expanduser().resolve()
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 2
    pdfs = _find_pdfs(root)
    if args.limit is not None:
        pdfs = pdfs[: max(0, args.limit)]
    if not pdfs:
        print(f"no PDFs under {root}", file=sys.stderr)
        return 2

    s = Settings(_env_file=None)
    limits = runner.SandboxLimits.from_settings(s)
    scratch_root = Path(tempfile.mkdtemp(prefix="rfp-smoke-"))
    images_root = scratch_root / "images"
    print(f"sandbox {protocol.SANDBOX_VERSION} / protocol {protocol.PROTOCOL_VERSION}; "
          f"limits_hash {limits.limits_hash()[:12]}; scratch {scratch_root}; "
          f"{len(pdfs)} PDF(s) under {root}")
    print(f"timeouts: open {s.rfp_ingest_open_timeout_seconds}s, stall "
          f"{s.rfp_ingest_page_stall_seconds}s, file {_file_timeout_seconds(s)}s; "
          f"tiers thumb {limits.thumb_long_side}px / full {limits.full_small_long_side}px "
          f"(<= {limits.full_small_threshold_pt}pt) or {limits.full_long_side}px")
    header = (
        f"{'file':<{_NAME_WIDTH}} {'verdict':<24} {'pages':>5} {'ok':>4} {'fail':>4} "
        f"{'rst':>3} {'elapsed':>8} {'thumb':>7} {'full':>7} {'rss_kb':>8}  bounds"
    )
    print(header)
    print("-" * len(header))

    bugs: list[str] = []
    verdicts: dict[str, int] = {}
    images_rows: list[str] = []
    started = time.monotonic()
    for pdf in pdfs:
        writer = ImagesPdfWriter(images_root / pdf.stem,
                                 part_bytes=s.rfp_ingest_images_pdf_part_bytes)
        sink = _ImagesSink(writer)
        try:
            with writer:
                result = _run_one(pdf, s=s, limits=limits, scratch_root=scratch_root,
                                  page_sink=sink)
        except runner.SandboxSpawnError as exc:
            verdict = f"{runner.STATUS_FAILED}/{protocol.FAIL_SPAWN}"
            print(f"{_short(pdf.name):<{_NAME_WIDTH}} {verdict:<24} (parent) {exc}")
            bugs.append(f"{pdf.name}: {verdict}")
            verdicts[verdict] = verdicts.get(verdict, 0) + 1
            continue
        verdict = result.status if result.code is None else f"{result.status}/{result.code}"
        verdicts[verdict] = verdicts.get(verdict, 0) + 1
        if (result.status, result.code) in _BUG_VERDICTS:
            bugs.append(f"{pdf.name}: {verdict}")
        doc = result.document or {}
        page_count = doc.get("page_count") if doc.get("event") != protocol.EVENT_REJECT else None
        if page_count is None and result.pages:
            page_count = len(result.pages)
        ok_pages = [p for p in result.pages if p.status == protocol.PAGE_OK]
        failed_pages = [p for p in result.pages if p.status != protocol.PAGE_OK]
        thumb_bytes = sum(int(p.thumb_meta["bytes"]) for p in ok_pages if p.thumb_meta)
        full_bytes = sum(int(p.full_meta["bytes"]) for p in ok_pages if p.full_meta)
        print(
            f"{_short(pdf.name):<{_NAME_WIDTH}} {verdict:<24} "
            f"{'-' if page_count is None else page_count:>5} {len(ok_pages):>4} "
            f"{len(failed_pages):>4} {result.restarts:>3} {result.elapsed_ms / 1000:>7.1f}s "
            f"{_fmt_bytes(thumb_bytes):>7} {_fmt_bytes(full_bytes):>7} "
            f"{'-' if result.peak_rss_kb is None else result.peak_rss_kb:>8}  "
            f"{','.join(result.bounds_hit) or '-'}"
        )
        for page in failed_pages:
            print(f"    page {page.index}: {page.code} ({page.detail})")
        if result.stderr_tail:
            for line in result.stderr_tail.splitlines():
                print(f"    stderr: {line}")
        if result.status == runner.STATUS_COMPLETE and ok_pages:
            if sink.error is not None:
                exc = sink.error
                images_rows.append(f"{_short(pdf.name):<{_NAME_WIDTH}} images PDF FAILED: "
                                   f"{type(exc).__name__}: {exc}")
                bugs.append(f"{pdf.name}: images PDF build raised {type(exc).__name__}")
                continue
            if sink.pages != len(ok_pages):
                bugs.append(f"{pdf.name}: the page sink saw {sink.pages} page(s), "
                            f"expected {len(ok_pages)}")
            parts = _parts_of(writer)
            for part, size, count in parts:
                images_rows.append(
                    f"{_short(pdf.name):<{_NAME_WIDTH}} {part.name:<16} "
                    f"{_fmt_bytes(size):>8} {'?' if count is None else count:>5} page(s)"
                )
            counts = [c for _p, _s, c in parts]
            if all(c is not None for c in counts) and sum(counts) != len(ok_pages):
                bugs.append(f"{pdf.name}: images PDF holds {sum(counts)} pages, "
                            f"expected {len(ok_pages)}")

    print()
    print("images PDF parts (thumb tier, pypdf page counts)")
    print("-" * len(header))
    for row in images_rows:
        print(row)
    print()
    print(f"{len(pdfs)} file(s) in {time.monotonic() - started:.1f}s; verdicts: "
          + ", ".join(f"{k} x{v}" for k, v in sorted(verdicts.items())))
    if bugs:
        print("BUG VERDICTS (child, sanitizer or runner):")
        for line in bugs:
            print(f"  {line}")
    if args.keep:
        print(f"kept scratch root {scratch_root} (images parts under {images_root})")
    else:
        shutil.rmtree(scratch_root, ignore_errors=True)
    return 1 if bugs else 0


if __name__ == "__main__":
    sys.exit(main())
