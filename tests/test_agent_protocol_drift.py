"""The worker vendors app/services/browser_agents/protocol.py byte for byte
(spec D17, 12.0 C1). Change the backend copy, then copy it over."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "app/services/browser_agents/protocol.py"
VENDORED = ROOT / "agent_worker/bdr_agent/protocol.py"


def test_vendored_protocol_is_byte_identical():
    assert VENDORED.is_file(), "agent_worker/bdr_agent/protocol.py is missing"
    assert SOURCE.read_bytes() == VENDORED.read_bytes(), (
        "protocol.py drifted: cp app/services/browser_agents/protocol.py agent_worker/bdr_agent/protocol.py"
    )


def test_protocol_imports_nothing_from_app():
    text = SOURCE.read_text(encoding="utf-8")
    assert not re.search(r"^\s*(from|import)\s+app\b", text, re.M)
    assert chr(0x2014) not in text and chr(0x2013) not in text
