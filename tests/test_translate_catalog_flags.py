"""scripts/translate_catalog.py flags (spec 0.2 and section 6 WS8):
--only-missing, --prefix, --sweep and --dry-run, at $0 with a fake
translator over temporary locale catalogs. No model call, no repo file
touched."""

from __future__ import annotations

import importlib.util
import json
import uuid
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "translate_catalog.py"


def _load():
    spec = importlib.util.spec_from_file_location("translate_catalog_under_test", _SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tc = _load()

EN = {
    "common": {"save": "Save", "cancel": "Cancel"},
    "aiEstimating": {
        "title": "AI Estimating",
        "chips": {"counted": "Counted {{q}}, marked {{m}}", "missing": "No price"},
        "autonomy": {"blocked": "Blocked"},
    },
    "aiEstimatingOld": {"x": "Not under the prefix"},
}
HI = {
    "common": {"save": "HI-save-old", "cancel": "HI-cancel-old"},
    "aiEstimating": {"title": "HI-title-old", "chips": {"counted": "HI-counted-old"}},
    "aiEstimatingOld": {"x": "HI-x-old"},
    "extraOnlyInTarget": {"k": "kept"},
}


class FakeTranslator:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, lang_name: str, code: str, batch: dict[str, str]) -> dict[str, str]:
        self.calls.append((code, dict(batch)))
        out = {k: f"{code.upper()}:{v}" for k, v in batch.items()}
        out["invented.key"] = "must be ignored"
        return out


def _write(root: Path, code: str, data: dict) -> Path:
    path = root / code / "translation.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


@pytest.fixture()
def locales(tmp_path: Path) -> Path:
    _write(tmp_path, "en", EN)
    _write(tmp_path, "hi", HI)
    return tmp_path


def _read(root: Path, code: str) -> dict:
    return json.loads((root / code / "translation.json").read_text(encoding="utf-8"))


def test_prefix_helpers_use_a_dotted_boundary():
    assert tc.normalize_prefix("aiEstimating.") == "aiEstimating"
    assert tc.normalize_prefix("  ") is None
    assert tc.under_prefix("aiEstimating.chips.missing", "aiEstimating")
    assert tc.under_prefix("aiEstimating", "aiEstimating")
    assert not tc.under_prefix("aiEstimatingOld.x", "aiEstimating")
    assert tc.under_prefix("anything", None)
    flat_en = tc.flatten(EN)
    flat_hi = tc.flatten(HI)
    assert tc.select_keys(flat_en, flat_hi, only_missing=True, prefix="aiEstimating") == [
        "aiEstimating.chips.missing",
        "aiEstimating.autonomy.blocked",
    ]
    assert tc.select_keys(flat_en, flat_hi, only_missing=False, prefix="aiEstimating") == [
        "aiEstimating.title",
        "aiEstimating.chips.counted",
        "aiEstimating.chips.missing",
        "aiEstimating.autonomy.blocked",
    ]
    assert tc.select_keys(flat_en, flat_hi, only_missing=True, prefix=None) == [
        "aiEstimating.chips.missing",
        "aiEstimating.autonomy.blocked",
    ]


def test_only_missing_with_prefix_translates_only_absent_keys_and_keeps_everything_else(locales: Path):
    before = (locales / "hi" / "translation.json").read_text(encoding="utf-8")
    fake = FakeTranslator()
    rc = tc.main(["hi", "--only-missing", "--prefix", "aiEstimating."], translator=fake, locales_dir=locales,
                 log=lambda _m: None)
    assert rc == 0
    assert [c for c, _b in fake.calls] == ["hi"]
    assert list(fake.calls[0][1]) == ["aiEstimating.chips.missing", "aiEstimating.autonomy.blocked"]
    after = _read(locales, "hi")
    # Existing strings unchanged, outside and inside the prefix.
    assert after["common"] == HI["common"]
    assert after["aiEstimatingOld"] == HI["aiEstimatingOld"]
    assert after["extraOnlyInTarget"] == {"k": "kept"}
    assert after["aiEstimating"]["title"] == "HI-title-old"
    assert after["aiEstimating"]["chips"]["counted"] == "HI-counted-old"
    # New keys added, translator extras ignored.
    assert after["aiEstimating"]["chips"]["missing"] == "HI:No price"
    assert after["aiEstimating"]["autonomy"] == {"blocked": "HI:Blocked"}
    assert "invented" not in after
    # Existing key order kept; new subtrees appended.
    assert list(after) == ["common", "aiEstimating", "aiEstimatingOld", "extraOnlyInTarget"]
    assert list(after["aiEstimating"]) == ["title", "chips", "autonomy"]
    # Every byte outside the selected keys is the same as before.
    flat_before = tc.flatten(json.loads(before))
    flat_after = tc.flatten(after)
    changed = {k for k in flat_after if flat_before.get(k) != flat_after[k]}
    assert changed == {"aiEstimating.chips.missing", "aiEstimating.autonomy.blocked"}

    # A second run has nothing left to translate.
    fake2 = FakeTranslator()
    assert tc.main(["hi", "--only-missing", "--prefix", "aiEstimating."], translator=fake2,
                   locales_dir=locales, log=lambda _m: None) == 0
    assert fake2.calls == []


def test_prefix_without_only_missing_retranslates_the_subtree_only(locales: Path):
    fake = FakeTranslator()
    assert tc.main(["hi", "--prefix", "aiEstimating"], translator=fake, locales_dir=locales,
                   log=lambda _m: None) == 0
    after = _read(locales, "hi")
    assert after["aiEstimating"]["title"] == "HI:AI Estimating"
    assert after["aiEstimating"]["chips"]["counted"] == "HI:Counted {{q}}, marked {{m}}"
    assert after["common"] == HI["common"] and after["aiEstimatingOld"] == HI["aiEstimatingOld"]
    assert after["extraOnlyInTarget"] == {"k": "kept"}


def test_only_missing_creates_a_missing_target_catalog(locales: Path):
    fake = FakeTranslator()
    assert tc.main(["sw", "--only-missing", "--prefix", "aiEstimating."], translator=fake, locales_dir=locales,
                   log=lambda _m: None) == 0
    sw = _read(locales, "sw")
    assert list(sw) == ["aiEstimating"]
    assert sw["aiEstimating"]["chips"]["missing"] == "SW:No price"


def test_whole_catalog_mode_still_replaces_the_file(locales: Path):
    fake = FakeTranslator()
    assert tc.main(["hi"], translator=fake, locales_dir=locales, log=lambda _m: None) == 0
    after = _read(locales, "hi")
    assert after["common"]["save"] == "HI:Save"
    assert "extraOnlyInTarget" not in after


def test_dry_run_lists_keys_and_writes_nothing(locales: Path):
    before = (locales / "hi" / "translation.json").read_bytes()
    lines: list[str] = []
    assert tc.main(["hi", "ur", "--only-missing", "--prefix", "aiEstimating.", "--dry-run"],
                   locales_dir=locales, log=lines.append) == 0
    assert (locales / "hi" / "translation.json").read_bytes() == before
    assert not (locales / "ur").exists()
    text = "\n".join(lines)
    assert "[hi] 2 key(s) to translate" in text and "[ur] 4 key(s) to translate" in text
    assert "aiEstimating.chips.missing" in text


def test_unknown_locale_and_catalog_conflicts_are_refused(locales: Path):
    assert tc.main(["xx"], translator=FakeTranslator(), locales_dir=locales, log=lambda _m: None) == 2
    target = {"aiEstimating": "a string where an object belongs"}
    with pytest.raises(ValueError, match="catalog conflict"):
        tc.merge_translations(target, {"aiEstimating.title": "x"})


def test_sweep_flag_arms_the_ledger_for_this_process(locales: Path, monkeypatch: pytest.MonkeyPatch):
    from app.core.config import get_settings

    monkeypatch.delenv("EVAL_SWEEP_ID", raising=False)
    monkeypatch.delenv("AI_EVAL_LEDGER_ENFORCE", raising=False)
    sweep = str(uuid.uuid4())
    try:
        assert tc.main(["hi", "--only-missing", "--prefix", "aiEstimating.", "--sweep", sweep],
                       translator=FakeTranslator(), locales_dir=locales, log=lambda _m: None) == 0
        import os

        assert os.environ["EVAL_SWEEP_ID"] == sweep
        assert os.environ["AI_EVAL_LEDGER_ENFORCE"] == "true"
        settings = get_settings()
        assert settings.eval_sweep_id == sweep and settings.ai_eval_ledger_enforce is True
    finally:
        monkeypatch.delenv("EVAL_SWEEP_ID", raising=False)
        monkeypatch.delenv("AI_EVAL_LEDGER_ENFORCE", raising=False)
        get_settings.cache_clear()
    with pytest.raises(SystemExit, match="one uuid"):
        tc.arm_sweep("not-a-uuid")


def test_scoped_live_run_refuses_without_a_sweep(locales: Path, monkeypatch: pytest.MonkeyPatch):
    """No translator injected = live: a scoped run with no sweep stops before
    any model client is built."""
    monkeypatch.delenv("EVAL_SWEEP_ID", raising=False)
    from app.services import llm

    def boom(*_a, **_k):
        raise AssertionError("no model call may happen")

    monkeypatch.setattr(llm, "complete_json", boom)
    lines: list[str] = []
    assert tc.main(["hi", "--only-missing", "--prefix", "aiEstimating."], locales_dir=locales,
                   log=lines.append) == 1
    assert any("--sweep" in m for m in lines)


def test_script_file_has_no_dashes():
    text = _SCRIPT.read_text(encoding="utf-8")
    assert chr(0x2013) not in text and chr(0x2014) not in text


def test_placeholders_select_keys_whose_target_text_is_the_english_text():
    select_keys = _load().select_keys

    en = {"aiEstimating.a": "Checks", "aiEstimating.b": "Review", "aiEstimating.c": "Scope", "other.x": "Other"}
    target = {"aiEstimating.a": "Checks", "aiEstimating.b": "Pagsusuri", "other.x": "Other"}
    assert select_keys(en, target, only_missing=False, prefix="aiEstimating", placeholders=True) == ["aiEstimating.a"]
    assert select_keys(en, target, only_missing=True, prefix="aiEstimating", placeholders=True) == ["aiEstimating.a", "aiEstimating.c"]
    assert select_keys(en, target, only_missing=True, prefix="aiEstimating") == ["aiEstimating.c"]
    assert select_keys(en, target, only_missing=False, prefix="aiEstimating") == ["aiEstimating.a", "aiEstimating.b", "aiEstimating.c"]
