"""Static check (spec 0.2, WS0): every live model call goes through
app/services/llm.py, so no module under app/ or scripts/ imports a provider
SDK or constructs a provider client, except the allowed transport and error
modules: app/services/llm.py, llm_anthropic.py, llm_errors.py (imports both
SDKs to classify their errors), llm_health.py and app/services/browser_agents/
(its own agent worker transport).

Parsed with ast, so a docstring that mentions OpenAI( never trips it, and an
aliased import or importlib.import_module("openai") still does."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS = ("app", "scripts")
SDK_MODULES = frozenset({"openai", "anthropic"})
CLIENT_NAMES = frozenset({"OpenAI", "AsyncOpenAI", "Anthropic", "AsyncAnthropic", "AzureOpenAI", "AsyncAzureOpenAI"})
ALLOWED_FILES = frozenset(
    {
        "app/services/llm.py",
        "app/services/llm_anthropic.py",
        "app/services/llm_errors.py",
        "app/services/llm_health.py",
    }
)
ALLOWED_PREFIXES = ("app/services/browser_agents/",)


def _allowed(rel: str) -> bool:
    return rel in ALLOWED_FILES or rel.startswith(ALLOWED_PREFIXES)


def _violations(source: str, rel: str) -> list[str]:
    out: list[str] = []
    tree = ast.parse(source, filename=rel)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in SDK_MODULES:
                    out.append(f"{rel}:{node.lineno} imports {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0 and node.module.split(".")[0] in SDK_MODULES:
                out.append(f"{rel}:{node.lineno} imports from {node.module}")
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
            if name in CLIENT_NAMES:
                out.append(f"{rel}:{node.lineno} constructs {name}(")
            if name in ("import_module", "__import__") and node.args:
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    if arg.value.split(".")[0] in SDK_MODULES:
                        out.append(f"{rel}:{node.lineno} imports {arg.value} dynamically")
    return out


def _files():
    for top in SCAN_DIRS:
        for path in sorted((ROOT / top).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            yield path, path.relative_to(ROOT).as_posix()


def test_no_provider_sdk_outside_the_allowed_modules():
    problems: list[str] = []
    scanned = 0
    for path, rel in _files():
        scanned += 1
        if _allowed(rel):
            continue
        problems.extend(_violations(path.read_text(encoding="utf-8"), rel))
    assert scanned > 100
    assert not problems, "provider SDK use outside llm.py (route it through app/services/llm.py):\n" + "\n".join(problems)


def test_the_detector_catches_every_form():
    src = (
        "import openai\n"
        "import anthropic as a\n"
        "from openai import OpenAI\n"
        "from anthropic.types import Message\n"
        "c = OpenAI(api_key='x')\n"
        "d = openai.AsyncOpenAI()\n"
        "e = a.Anthropic()\n"
        "import importlib\n"
        "m = importlib.import_module('openai')\n"
    )
    found = _violations(src, "app/services/probe.py")
    assert len(found) == 8, found
    clean = '"""Talks about OpenAI( and Anthropic( in prose only."""\nfrom app.services import llm\n'
    assert _violations(clean, "app/services/probe.py") == []


def test_the_allowed_modules_still_exist():
    for rel in ALLOWED_FILES:
        assert (ROOT / rel).is_file(), rel
    assert (ROOT / ALLOWED_PREFIXES[0]).is_dir()
