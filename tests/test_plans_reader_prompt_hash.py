"""services/plans_reader_prompt_hash.py (WS0 step 12): one stable hash over
every registered prompt part and tool definition plus the default system
prompts and schemas; ordering never matters, content always does."""

from __future__ import annotations

import pytest

from app.services import plans_reader_prompt_hash as ph


@pytest.fixture(autouse=True)
def clean():
    saved = ph.registered_parts()
    ph.clear_registered_parts()
    yield
    ph.clear_registered_parts()
    for name, text in saved.items():
        ph.register_prompt_part(name, text)


def test_order_of_registration_does_not_matter():
    ph.register_prompt_part("tool:b", "beta")
    ph.register_prompt_part("tool:a", {"y": 1, "x": [1, 2]})
    first = ph.prompt_hash(include_defaults=False)
    ph.clear_registered_parts()
    ph.register_prompt_part("tool:a", {"x": [1, 2], "y": 1})
    ph.register_prompt_part("tool:b", "beta")
    assert ph.prompt_hash(include_defaults=False) == first
    assert len(first) == 64


def test_content_and_names_change_the_hash():
    ph.register_prompt_part("tool:a", "alpha")
    base = ph.prompt_hash(include_defaults=False)
    ph.register_prompt_part("tool:a", "alpha ")
    assert ph.prompt_hash(include_defaults=False) != base
    ph.register_prompt_part("tool:a", "alpha")
    assert ph.prompt_hash(include_defaults=False) == base
    ph.unregister_prompt_part("tool:a")
    ph.register_prompt_part("tool:z", "alpha")
    assert ph.prompt_hash(include_defaults=False) != base
    with pytest.raises(ValueError):
        ph.register_prompt_part("  ", "x")


def test_defaults_cover_the_system_prompts_and_schemas():
    parts = ph.default_parts()
    assert "plans_reader_agents.SUB_SYSTEM" in parts
    assert "plans_reader_agents.MAIN_SYSTEM" in parts
    assert "plans_reader_schemas.SUB_AGENT_SCHEMA" in parts
    assert "plans_reader_schemas.MAIN_AGENT_SCHEMA" in parts
    assert not any(name.endswith("SNAPSHOT_SYSTEM_CHARS") for name in parts)
    assert ph.prompt_hash() == ph.prompt_hash()
    assert ph.prompt_hash() != ph.prompt_hash(include_defaults=False)


def test_registered_tools_reach_the_hash():
    from app.services import plans_reader_tools as tools

    before = ph.prompt_hash(include_defaults=False)
    spec = tools.ToolSpec(
        name="hash_probe_tool",
        description="A probe tool for the prompt hash test.",
        args_schema={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        result_doc={},
        mode="read",
        kinds=frozenset({"review"}),
        handler=lambda ctx, args: None,
    )
    tools._register_prompt_part(spec)
    assert "tool:hash_probe_tool" in ph.registered_parts()
    assert ph.prompt_hash(include_defaults=False) != before


def test_code_version_prefers_the_env(monkeypatch):
    ph.reset_code_version()
    monkeypatch.setenv("BDR_CODE_VERSION", "build-123")
    try:
        assert ph.code_version() == "build-123"
        monkeypatch.setenv("BDR_CODE_VERSION", "other")
        assert ph.code_version() == "build-123"  # read once per process
    finally:
        ph.reset_code_version()


def test_git_version_counts_untracked_files_as_dirty(monkeypatch):
    import subprocess

    calls = []

    class Done:
        def __init__(self, out):
            self.stdout = out

    def fake(cmd, *a, **k):
        calls.append(cmd)
        if "rev-parse" in cmd:
            return Done("abc123\n")
        return Done(status)

    monkeypatch.setattr(subprocess, "run", fake)
    status = "?? app/services/new_module.py\n"
    assert ph._git_version() == "abc123-dirty"
    status_cmd = next(c for c in calls if "status" in c)
    assert "--untracked-files=normal" in status_cmd and "--untracked-files=no" not in status_cmd
    status = ""
    assert ph._git_version() == "abc123"


def test_consolidator_prompts_reach_the_hash():
    """WS4: the consolidator system prompt and its first-turn instructions
    are registered parts, so a prompt change moves the run snapshot's
    prompt_hash."""
    from app.services import plans_reader_consolidator as C
    ph.clear_registered_parts()  # the import-time registration must not land in before

    before = ph.prompt_hash(include_defaults=False)
    C.register_prompt_parts()
    parts = ph.registered_parts()
    assert parts["ws4.consolidator_system"] == C.CONSOLIDATOR_SYSTEM
    assert parts["ws4.consolidator_instructions"] == C.INSTRUCTIONS
    # the 10-22 luminaire variant and gear naming sentences are in the hashed part
    assert "AE beside A, keeps that tag with no suffix" in parts["ws4.consolidator_system"]
    assert "75KVA TRANSFORMER" in parts["ws4.consolidator_system"]
    assert "never cited under an equipment connection line" in parts["ws4.consolidator_system"]
    registered = ph.prompt_hash(include_defaults=False)
    assert registered != before
    ph.register_prompt_part("ws4.consolidator_instructions", C.INSTRUCTIONS + "\n6. Changed.")
    assert ph.prompt_hash(include_defaults=False) != registered
    C.register_prompt_parts()
    assert ph.prompt_hash(include_defaults=False) == registered


def test_consolidator_registers_its_prompts_at_import():
    import subprocess
    import sys
    from pathlib import Path

    code = (
        "from app.services import plans_reader_prompt_hash as ph\n"
        "import app.services.plans_reader_consolidator\n"
        "names = ph.registered_parts()\n"
        "print(int('ws4.consolidator_system' in names and 'ws4.consolidator_instructions' in names))\n"
    )
    root = Path(__file__).resolve().parents[1]
    out = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip().splitlines()[-1] == "1"
