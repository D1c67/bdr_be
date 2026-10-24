"""No en dash (U+2013) or em dash (U+2014) in anything the AI Estimating
build adds (spec 0.2): every path listed in tests/ai_estimating_new_files.txt
(one path per line, relative to the BDR folder that holds bdr_be and bdr_fe,
both repos) plus the aiEstimating subtree of each of the six locale files.
Existing files outside the list are not scanned (they predate the rule).
A listed path that does not exist fails the test: fix the manifest line or
restore the file."""

from __future__ import annotations

import json
from pathlib import Path

BE_ROOT = Path(__file__).resolve().parents[1]
BDR_ROOT = BE_ROOT.parent
MANIFEST = BE_ROOT / "tests" / "ai_estimating_new_files.txt"
LOCALES = ("en", "ceb", "fil", "hi", "sw", "ur")
EN_DASH = chr(0x2013)
EM_DASH = chr(0x2014)
DASHES = {EN_DASH: "en dash U+2013", EM_DASH: "em dash U+2014"}


def _manifest_paths() -> list[str]:
    out: list[str] = []
    for raw in MANIFEST.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and line not in out:
            out.append(line)
    return out


def _dash_hits(text: str, label: str) -> list[str]:
    hits: list[str] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for ch, name in DASHES.items():
            if ch in line:
                hits.append(f"{label}:{lineno}: {name}: {line.strip()[:120]}")
    return hits


def _walk_strings(node, path: str):
    if isinstance(node, dict):
        for key, value in node.items():
            yield f"{path}.{key}", str(key)
            yield from _walk_strings(value, f"{path}.{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from _walk_strings(value, f"{path}[{i}]")
    elif isinstance(node, str):
        yield path, node


def test_manifest_exists_and_lists_this_test():
    assert MANIFEST.is_file(), f"missing manifest {MANIFEST}"
    assert "bdr_be/tests/test_no_dashes_ai_estimating.py" in _manifest_paths()


def test_listed_files_have_no_en_or_em_dashes():
    missing: list[str] = []
    hits: list[str] = []
    for rel in _manifest_paths():
        path = BDR_ROOT / rel
        if not path.is_file():
            missing.append(rel)
            continue
        text = path.read_bytes().decode("utf-8", errors="replace")
        hits.extend(_dash_hits(text, rel))
    assert not missing, (
        f"{len(missing)} path(s) in {MANIFEST.name} do not exist (paths are relative to {BDR_ROOT}):\n"
        + "\n".join(missing)
    )
    assert not hits, "en or em dashes found (use a comma, colon, parentheses or a plain hyphen):\n" + "\n".join(hits)


def test_locale_ai_estimating_subtrees_have_no_dashes():
    missing: list[str] = []
    hits: list[str] = []
    for lang in LOCALES:
        path = BDR_ROOT / "bdr_fe" / "locales" / lang / "translation.json"
        if not path.is_file():
            missing.append(str(path))
            continue
        catalog = json.loads(path.read_text(encoding="utf-8"))
        subtree = catalog.get("aiEstimating")
        if subtree is None:
            missing.append(f"{path} (no aiEstimating key)")
            continue
        for key_path, text in _walk_strings(subtree, f"{lang}:aiEstimating"):
            for ch, name in DASHES.items():
                if ch in text:
                    hits.append(f"{key_path}: {name}: {text[:120]}")
    assert not missing, "locale files missing:\n" + "\n".join(missing)
    assert not hits, "en or em dashes in the aiEstimating locale subtree:\n" + "\n".join(hits)


def test_detector_sees_both_dashes():
    sample = f"a {EN_DASH} b\nc {EM_DASH} d\nplain - hyphen"
    assert len(_dash_hits(sample, "x")) == 2
