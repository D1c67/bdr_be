"""Worker token at rest (spec 8, 9).

Windows: DPAPI (CryptProtectData) via ctypes, current-user scope. The service
runs as the standard local user `bdr-agent`, so enrollment must run as that
user too (the installer does), otherwise the service cannot decrypt it.
Elsewhere (dev Macs, CI): a plain file created 0600; a file readable by group
or others is refused.

File format: a 7-byte tag then the payload.
    b"DPAPI1:" + DPAPI blob
    b"PLAIN1:" + token utf-8
Writes are atomic with a .bak copy (atomicfile).
"""

import ctypes
import os
import stat
import sys
from pathlib import Path
from typing import Protocol

from .atomicfile import atomic_write_bytes, bak_path
from .protocol import WORKER_TOKEN_RE

TAG_DPAPI = b"DPAPI1:"
TAG_PLAIN = b"PLAIN1:"
# Ties the blob to this purpose; another DPAPI user cannot replay it elsewhere.
DPAPI_ENTROPY = b"bdr-agent-worker-token-v1"


class TokenStoreError(RuntimeError):
    pass


class Protector(Protocol):
    tag: bytes

    def protect(self, data: bytes) -> bytes: ...

    def unprotect(self, blob: bytes) -> bytes: ...


class PlainProtector:
    tag = TAG_PLAIN

    def protect(self, data: bytes) -> bytes:
        return data

    def unprotect(self, blob: bytes) -> bytes:
        return blob


class DpapiProtector:
    """CryptProtectData / CryptUnprotectData, user scope, no UI."""

    tag = TAG_DPAPI
    CRYPTPROTECT_UI_FORBIDDEN = 0x1

    def __init__(self) -> None:
        if sys.platform != "win32":  # pragma: no cover - exercised on Windows only
            raise TokenStoreError("DPAPI is only available on Windows")
        from ctypes import wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

        self._blob_t = DATA_BLOB
        self._crypt32 = ctypes.windll.crypt32  # type: ignore[attr-defined]
        self._kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

    def _in(self, data: bytes):  # pragma: no cover - Windows only
        buf = ctypes.create_string_buffer(data, len(data))
        blob = self._blob_t(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        return blob, buf

    def _out(self, blob) -> bytes:  # pragma: no cover - Windows only
        try:
            return ctypes.string_at(blob.pbData, blob.cbData)
        finally:
            self._kernel32.LocalFree(blob.pbData)

    def protect(self, data: bytes) -> bytes:  # pragma: no cover - Windows only
        src, _keep1 = self._in(data)
        ent, _keep2 = self._in(DPAPI_ENTROPY)
        out = self._blob_t()
        ok = self._crypt32.CryptProtectData(
            ctypes.byref(src), "bdr-agent", ctypes.byref(ent), None, None,
            self.CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out))
        if not ok:
            raise TokenStoreError(f"CryptProtectData failed ({ctypes.GetLastError()})")  # type: ignore[attr-defined]
        return self._out(out)

    def unprotect(self, blob: bytes) -> bytes:  # pragma: no cover - Windows only
        src, _keep1 = self._in(blob)
        ent, _keep2 = self._in(DPAPI_ENTROPY)
        out = self._blob_t()
        ok = self._crypt32.CryptUnprotectData(
            ctypes.byref(src), None, ctypes.byref(ent), None, None,
            self.CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out))
        if not ok:
            raise TokenStoreError(f"CryptUnprotectData failed ({ctypes.GetLastError()})")  # type: ignore[attr-defined]
        return self._out(out)


def default_protector() -> Protector:
    if sys.platform == "win32":  # pragma: no cover - Windows only
        return DpapiProtector()
    return PlainProtector()


class TokenStore:
    def __init__(self, path: Path, protector: Protector | None = None):
        self.path = Path(path)
        self.protector = protector or default_protector()

    def save(self, token: str) -> None:
        if not WORKER_TOKEN_RE.match(token or ""):
            raise TokenStoreError("refusing to store a malformed worker token")
        blob = self.protector.protect(token.encode("utf-8"))
        atomic_write_bytes(self.path, self.protector.tag + blob, mode=0o600)

    def _check_mode(self, path: Path) -> None:
        if sys.platform == "win32" or self.protector.tag != TAG_PLAIN:
            return
        try:
            st = path.stat()
        except FileNotFoundError:
            return
        if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise TokenStoreError(f"{path} is readable by other users; expected mode 0600")

    def _parse(self, raw: bytes) -> str:
        tag, payload = raw[:7], raw[7:]
        if tag != self.protector.tag:
            raise TokenStoreError("token file was written by a different protector")
        token = self.protector.unprotect(payload).decode("utf-8")
        if not WORKER_TOKEN_RE.match(token):
            raise TokenStoreError("stored worker token is malformed")
        return token

    def load(self) -> str | None:
        """The token, or None when not enrolled. Corrupt primary falls back to .bak."""
        last_error: Exception | None = None
        for candidate in (self.path, bak_path(self.path)):
            self._check_mode(candidate)
            try:
                raw = candidate.read_bytes()
            except FileNotFoundError:
                continue
            try:
                return self._parse(raw)
            except Exception as exc:   # corrupt or foreign: try the .bak
                last_error = exc
        if last_error is not None:
            # A file exists but neither copy is usable: say so rather than
            # pretend the PC was never enrolled.
            raise TokenStoreError(f"worker token unreadable: {last_error}")
        return None

    def clear(self) -> None:
        for candidate in (self.path, bak_path(self.path)):
            try:
                os.remove(candidate)
            except FileNotFoundError:
                pass
