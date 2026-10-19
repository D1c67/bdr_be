"""Upload scan hook for helper files (docs/browser_agents/SPEC.md 6 step 4).

Every file a helper uploads to a help request passes through
`scan_upload` after the size cap and the magic-byte sniff, before it is
stored. v1 has no scanner, so this is a no-op seam: a future malware or
content scanner raises ScanRejected with a short fixed reason (never file
content), and the upload is refused with file_not_allowed.
"""

from __future__ import annotations


class ScanRejected(ValueError):
    """The scanner refused the file; the message is a short fixed reason."""


def scan_upload(data: bytes, filename: str, mime_type: str) -> None:
    """No-op in v1. Raise ScanRejected to refuse the upload."""
    return None
