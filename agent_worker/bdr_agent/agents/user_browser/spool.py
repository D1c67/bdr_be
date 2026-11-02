"""Bounded memory-only personal logs. A restart loses pending steps, never spills them."""
import json
from pathlib import Path
from uuid import UUID

from ...spool import JobSpool, Spool, StepLog, FLUSH_RETRY
from .client import UserSyncClient

MAX_MEMORY_BYTES = 8 * 1024 * 1024


class MemoryJobSpool(JobSpool):
    def __init__(self, job_id: UUID, epoch: int):
        super().__init__(Path("."), job_id, epoch)
        self._loaded = True
        self._bytes = 0

    def _load_locked(self):
        pass

    def _write_locked(self, records):
        size = sum(len(json.dumps(row, default=str).encode()) for row in records)
        if self._bytes + size > MAX_MEMORY_BYTES:
            raise RuntimeError("Personal task log memory cap reached")
        self._bytes += size

    def ack(self, idem_keys):
        # Bound memory over long help waits; retain keys for same-process idempotency.
        super().ack(idem_keys)
        with self._lock:
            for key in idem_keys:
                row = self._rows.pop(key, None)
                if row is not None:
                    self._bytes = max(0, self._bytes - len(json.dumps({"s": row}, default=str).encode()))

    def remove_if_settled(self):
        with self._lock:
            if not self._done or self._rows:
                return False
            self._rows.clear()
            self._acked.clear()
            self._bytes = 0
            return True


class MemorySpool(Spool):
    def __init__(self):
        import threading
        self._lock = threading.Lock()
        self._open = {}

    def for_job(self, job_id, epoch):
        key = (UUID(str(job_id)), int(epoch))
        with self._lock:
            if key not in self._open:
                self._open[key] = MemoryJobSpool(*key)
            return self._open[key]

    def on_disk(self):
        return []


class UserStepLog(StepLog):
    def flush_sync(self, client, *, retry=FLUSH_RETRY):
        # The heartbeat thread owns the common client. Its stall traces must
        # also use the encrypted endpoint, never the org step ledger.
        return super().flush_sync(UserSyncClient(client), retry=retry)
