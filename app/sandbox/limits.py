"""Resource limits for the sandbox child, applied before anything untrusted runs.

The child calls `apply()` as the very first thing after opening its progress
log and BEFORE importing pypdfium2 or Pillow, so that the PDF parser and the
JPEG encoder only ever execute under the bounds the parent asked for. Every
limit is set as (soft, hard) = (value, value): the child cannot raise them
back, and on Linux a child that is not root cannot exceed them at all.

What gets applied, in this order, each in its own try/except so one refusal
never blocks the rest:

  core    RLIMIT_CORE    0 (a crash never writes a core file into <out>)
  nproc   RLIMIT_NPROC   limits.max_processes (1 = the child cannot fork)
  nofile  RLIMIT_NOFILE  limits.max_open_files
  memory  RLIMIT_AS      limits.memory_bytes
  cpu     RLIMIT_CPU     limits.cpu_seconds (SIGXCPU at the soft limit, which
                         terminates the process by default; SIGKILL at hard)
  fsize   RLIMIT_FSIZE   limits.max_output_file_bytes (the largest single
                         artifact; Python ignores SIGXFSZ so a write past it
                         fails with EFBIG and surfaces as an OSError)

The result is recorded verbatim in the `start` event as `limits_applied`:
one boolean per label (True only when setrlimit succeeded AND a getrlimit
readback shows soft == hard == value) plus `readback`, the [soft, hard] pair
actually in force after the attempt (RLIM_INFINITY rendered as null, and null
for the whole pair when the platform has no such resource). The parent uses
`limits_applied.memory` to decide whether a SIGKILL/SIGABRT after a
`page_start` is blamed on memory.

Platform notes, both verified on the development machine: macOS refuses
RLIMIT_AS with ValueError ("current limit exceeds maximum limit") and the
child records memory=False there, while CORE, NPROC, NOFILE, CPU and FSIZE
apply. Linux applies all six. RLIMIT_RSS is deliberately not used (a no-op
on modern kernels). `ru_maxrss` is bytes on macOS and kilobytes on Linux;
`peak_rss_kb()` normalizes so the `end` event always reports kilobytes.

This module imports only the standard library.
"""

from __future__ import annotations

import resource
import sys

# (label, resource constant name, limits-document key, fixed value). A fixed
# value is used when the key is None. Order is the application order.
RLIMIT_PLAN: tuple[tuple[str, str, str | None, int], ...] = (
    ("core", "RLIMIT_CORE", None, 0),
    ("nproc", "RLIMIT_NPROC", "max_processes", 0),
    ("nofile", "RLIMIT_NOFILE", "max_open_files", 0),
    ("memory", "RLIMIT_AS", "memory_bytes", 0),
    ("cpu", "RLIMIT_CPU", "cpu_seconds", 0),
    ("fsize", "RLIMIT_FSIZE", "max_output_file_bytes", 0),
)
LABELS: tuple[str, ...] = tuple(label for label, _c, _k, _f in RLIMIT_PLAN)


def _json_limit(value: int | None) -> int | None:
    """RLIM_INFINITY (and a failed readback) become null so the parent's
    number validation never meets a -1 or a platform-specific sentinel."""
    if value is None or value == resource.RLIM_INFINITY or value < 0:
        return None
    return int(value)


def apply(limits: dict) -> dict:
    """Apply every rlimit in RLIMIT_PLAN as (v, v) and return the
    `limits_applied` document: `{label: bool, ..., "readback": {label: [soft,
    hard] | null}}`. Never raises: a resource the platform lacks or refuses is
    simply recorded as not applied."""
    applied: dict[str, object] = {}
    readback: dict[str, list[int | None] | None] = {}
    for label, const_name, key, fixed in RLIMIT_PLAN:
        value = fixed if key is None else int(limits[key])
        res = getattr(resource, const_name, None)
        if res is None:
            applied[label] = False
            readback[label] = None
            continue
        ok = True
        try:
            resource.setrlimit(res, (value, value))
        except (OSError, ValueError):
            ok = False
        try:
            soft, hard = resource.getrlimit(res)
        except (OSError, ValueError):
            soft = hard = None
            ok = False
        else:
            ok = ok and soft == value and hard == value
        applied[label] = ok
        readback[label] = [_json_limit(soft), _json_limit(hard)]
    applied["readback"] = readback
    return applied


def peak_rss_kb() -> int:
    """Peak resident set size of THIS process in kilobytes, whatever the
    platform's getrusage unit is."""
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return int(value // 1024)
    return int(value)
