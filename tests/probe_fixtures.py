"""Fixtures of the wave 4 dev_probe driver tests
(tests/test_scope_attrib_probe.py, tests/test_pricing_quote_fallback_probe.py).

A synthetic production backup on disk (data/projects.json,
data/project_files.json, storage/project-files/...) whose projects carry
real golden ids (so the selection runs through eval_manifest), one-page
text-layer PDFs, a scripted `llm.complete_turn` and the in-memory ledger.
No network, no database, no model; amounts are synthetic.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from collections.abc import Callable, Iterable
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.services import eval_ledger, llm, llm_parts

BE_ROOT = Path(__file__).resolve().parents[1]
DEV_URL = "https://bpidntbyvoooqvaispup.supabase.co"
PROD_URL = "https://zxqkqotlcgbalwoqeeca.supabase.co"
ARMED = ("EVAL_SWEEP_ID", "AI_EVAL_LEDGER_ENFORCE", "LLM_CASSETTE_MODE", "LLM_CASSETTE_DIR")

# Golden ids (eval_manifest.GOLDEN_IDS): smoke 7154 and 7177, core 7181,
# holdout 7188, pool 026b2dc5.
P7154 = "7dfc64e6-36ef-4c67-9d02-0d7942015011"
P7177 = "ba167b17-0ff5-4012-a638-403bcced09af"
P7181 = "2bf3b21e-5ce4-406c-aafe-592dbc7d00e7"
P7188 = "078c470d-efa1-479c-b579-1690f5da47a0"
POOL1 = "026b2dc5-676f-4ebd-b9a2-b744238b4049"


def load_script(name: str) -> Any:
    """Import scripts/<name>.py as a module (cached in sys.modules)."""
    if name in sys.modules:
        return sys.modules[name]
    path = BE_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def settings(**over: Any) -> Settings:
    base = {"openai_api_key": "test-key", "supabase_url": DEV_URL, "plans_reader_enabled": True}
    base.update(over)
    return Settings(_env_file=None, **base)


def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(lines: Iterable[str], *, width: int = 612, height: int = 792) -> bytes:
    """A one-page PDF whose text layer holds `lines`, one per text line."""
    content = "BT /F1 10 Tf 40 760 Td 13 TL " + " ".join(f"({_esc(ln)}) Tj T*" for ln in lines) + " ET"
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] /Contents 4 0 R "
        "/Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets: list[int] = []
    for i, obj in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{obj}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{off:010d} 00000 n \n".encode() for off in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


class FakeBackup:
    """Writes a backup tree under `root` and hands it to BackupData."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.projects: list[dict[str, Any]] = []
        self.files: list[dict[str, Any]] = []

    def project(self, pid: str, name: str = "Test project") -> None:
        self.projects.append({"id": pid, "name": name, "number": "26.1.0001", "wage_type": None})

    def file(self, pid: str, category: str, filename: str, data: bytes, *, file_id: str | None = None) -> str:
        fid = file_id or f"{len(self.files) + 1:08d}-0000-4000-8000-000000000000"
        storage = f"{pid}/{category}/{filename}"
        path = self.root / "storage" / "project-files" / storage
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        self.files.append({"id": fid, "project_id": pid, "category": category, "filename": filename,
                           "storage_path": storage, "created_at": f"2026-10-01T00:00:{len(self.files):02d}Z"})
        return fid

    def data(self) -> Any:
        from app.services.eval_reference import BackupData

        d = self.root / "data"
        d.mkdir(parents=True, exist_ok=True)
        (d / "projects.json").write_text(json.dumps(self.projects))
        (d / "project_files.json").write_text(json.dumps(self.files))
        return BackupData(self.root)


def open_sweep(*, workstream: str, purpose: str = "dev_probe", budget: str = "3") -> dict[str, Any]:
    eval_ledger.use_store(eval_ledger.MemoryLedgerStore())
    return eval_ledger.open_sweep(label="probe test", workstream=workstream, purpose=purpose,
                                  budget_usd=Decimal(budget))


def scripted_turn(answer: Callable[[llm_parts.ModelRequest], Any], *, cost: str = "0.05",
                  calls: list | None = None) -> Callable[..., llm_parts.ModelTurn]:
    """A complete_turn stand-in: records the request, publishes the cost
    through llm.last_cost_usd() as the real guard does, returns `answer`."""

    def _turn(feature: str, req: llm_parts.ModelRequest, *, settings: Any = None) -> llm_parts.ModelTurn:
        if calls is not None:
            calls.append((feature, req))
        llm._last_cost.set(Decimal(cost))
        return llm_parts.ModelTurn(text="", parsed=answer(req), tool_calls=[], usage=None, cost_usd=Decimal(cost),
                                   response_id="resp-1", stop_reason="final", provider="openai", model="gpt-6-astra")

    return _turn


def restore_env(saved: dict[str, str | None]) -> None:
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


__all__ = [
    "ARMED", "DEV_URL", "FakeBackup", "P7154", "P7177", "P7181", "P7188", "POOL1", "PROD_URL", "load_script",
    "make_pdf", "open_sweep", "restore_env", "scripted_turn", "settings",
]
