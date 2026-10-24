#!/usr/bin/env python
"""Translator for the frontend i18n catalog (re-runnable).

Reads the English source catalog (bdr_fe/locales/en/translation.json) and
writes translated strings into each supported non-English locale, reusing the
app's LLM routing (services/llm, feature "translate"; the self-hosted endpoint
when FULL_SELF_HOSTED_LLMS_ENABLED=true; no new dependency, no infra). Output
is committed to the repo: there is NO runtime translation.

Usage (from bdr_be/, with the provider key or the self-hosted vars in .env):
    python scripts/translate_catalog.py                      # whole catalog, all locales
    python scripts/translate_catalog.py hi ur                # whole catalog, given locales
    python scripts/translate_catalog.py --only-missing --prefix aiEstimating. \\
        --sweep <open sweep id>                              # new AI Estimating keys only
    python scripts/translate_catalog.py --only-missing --prefix aiEstimating. --dry-run

Flags (AI Estimating build, docs/AI_ESTIMATING_BUILD_SPEC.md 0.2 and WS8):
- `--only-missing`: translate only keys that are absent from the target
  locale; every existing target string is kept byte for byte.
- `--prefix <dotted.key.prefix>`: restrict the work to keys under the prefix
  ("aiEstimating." or "aiEstimating" both mean the aiEstimating subtree). Keys
  outside the prefix are never rewritten: the target catalog is loaded, only
  the selected keys are replaced or added, and the rest is written back as it
  was (same order, same values).
- `--sweep <id>`: the open eval ledger sweep the run is attributed to. It sets
  EVAL_SWEEP_ID and AI_EVAL_LEDGER_ENFORCE=true for this process, so every
  call is admitted, capped and attributed by app/services/eval_ledger.py
  (spec 0.4 rule 2: live scripts run in their own process inside a sweep).
  With --only-missing or --prefix a live run refuses to start without it
  (or without EVAL_SWEEP_ID already set).
- `--dry-run`: print what would be translated per locale and exit; no model
  call, no file written.

Without --only-missing and --prefix the script behaves as before: the whole
catalog is re-translated and each target file is replaced.

Notes:
- Interpolation placeholders ({{name}}) and brand or technical tokens (G3,
  BDR, RFQ, BOQ, PDF, PM, PE, PA, GC) are preserved verbatim.
- Cebuano (ceb) is the weakest-supported language for every engine, and Urdu
  (ur) is RTL: both should get a native-speaker review pass before being
  relied on.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

# Make `app` importable when run as a plain script from bdr_be/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# bdr_be/scripts/ -> repo root -> bdr_fe/locales
LOCALES_DIR = Path(__file__).resolve().parent.parent.parent / "bdr_fe" / "locales"
SOURCE_LOCALE = "en"

# code -> human name fed to the model (autonym in parens for clarity).
TARGETS: dict[str, str] = {
    "fil": "Filipino (Tagalog)",
    "ceb": "Cebuano (Binisaya)",
    "sw": "Swahili (Kiswahili)",
    "hi": "Hindi (हिन्दी)",
    "ur": "Urdu (اردو)",
}

# Tokens that must survive untranslated.
DO_NOT_TRANSLATE = ["G3", "BDR", "RFQ", "RFQs", "BOQ", "PDF", "PM", "PE", "PA", "GC"]

# Keep each request comfortably small so the model returns complete, valid JSON.
BATCH_SIZE = 120

# (lang_name, code, batch) -> translated batch with the same keys.
Translator = Callable[[str, str, dict[str, str]], dict[str, str]]


def flatten(obj: Any, prefix: str = "") -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in obj.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            out.update(flatten(value, path))
        else:
            out[path] = value
    return out


def unflatten(flat: dict[str, str]) -> dict[str, Any]:
    root: dict[str, Any] = {}
    for path, value in flat.items():
        parts = path.split(".")
        node = root
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return root


def normalize_prefix(prefix: str | None) -> str | None:
    """'aiEstimating.' and 'aiEstimating' both select the aiEstimating
    subtree; an empty prefix selects everything (None)."""
    p = (prefix or "").strip()
    if not p:
        return None
    return p.rstrip(".")


def under_prefix(key: str, prefix: str | None) -> bool:
    """True when `key` is the prefix itself or a key below it (a dotted
    boundary: prefix 'aiEstimating' never selects 'aiEstimatingX.y')."""
    if prefix is None:
        return True
    return key == prefix or key.startswith(prefix + ".")


def select_keys(
    flat_en: dict[str, str],
    flat_target: dict[str, str] | None,
    *,
    only_missing: bool,
    prefix: str | None,
    placeholders: bool = False,
) -> list[str]:
    """English keys to translate for one locale, in English catalog order.
    `placeholders` selects the keys whose target text is the English text
    (an English placeholder a build copied into the file), on top of the
    missing keys when both flags are given."""
    keys = [k for k in flat_en if under_prefix(k, prefix)]
    have = flat_target or {}
    if only_missing and placeholders:
        keys = [k for k in keys if k not in have or have.get(k) == flat_en[k]]
    elif only_missing:
        keys = [k for k in keys if k not in have]
    elif placeholders:
        keys = [k for k in keys if k in have and have.get(k) == flat_en[k]]
    return keys


def set_path(root: dict[str, Any], path: str, value: str) -> None:
    """Set a dotted key in a nested catalog, creating missing objects at the
    end of their parent (existing key order is kept). A string where an
    object is needed is a catalog conflict and raises."""
    parts = path.split(".")
    node = root
    for part in parts[:-1]:
        child = node.get(part)
        if child is None:
            child = {}
            node[part] = child
        elif not isinstance(child, dict):
            raise ValueError(f"catalog conflict at {path!r}: {part!r} is a string, not an object")
        node = child
    if isinstance(node.get(parts[-1]), dict):
        raise ValueError(f"catalog conflict at {path!r}: the target holds an object there")
    node[parts[-1]] = value


def merge_translations(target: dict[str, Any], translated: dict[str, str]) -> dict[str, Any]:
    """The target catalog with only the translated keys replaced or added.
    Nothing else changes (deep copy through JSON, so the caller's dict is
    untouched)."""
    out = json.loads(json.dumps(target, ensure_ascii=False))
    for path, value in translated.items():
        set_path(out, path, value)
    return out


def _validate(data: Any) -> dict[str, str]:
    if not isinstance(data, dict):
        raise ValueError("Model response was not a JSON object")
    return data


def _system_prompt(lang_name: str, code: str) -> str:
    return (
        f"You are a professional software-UI localizer translating from English (en) "
        f"into {lang_name} (locale code '{code}'). The app is a construction electrical "
        f"bidding workspace used by office staff.\n\n"
        "Rules:\n"
        "- Return ONLY a single JSON object: the SAME keys as the input, each value "
        "translated. No markdown, no commentary.\n"
        "- Translate naturally and concisely so text fits UI buttons/labels.\n"
        "- Keep interpolation placeholders EXACTLY as written, e.g. {{name}}, {{count}}; "
        "never translate or reorder their inner text.\n"
        f"- Do NOT translate these tokens, keep them verbatim: {', '.join(DO_NOT_TRANSLATE)}.\n"
        "- Preserve leading/trailing whitespace, ellipses (…), and punctuation.\n"
        "- Never use em dashes or en dashes; use a comma, a colon or a plain hyphen.\n"
        "- Keep the meaning faithful; do not add or drop information."
    )


def translate_batch(lang_name: str, code: str, batch: dict[str, str]) -> dict[str, str]:
    """The live translator: one llm.complete_json call per batch (through
    app/services/llm.py, so it is logged, guarded and priced)."""
    from app.services import llm

    out = _validate(
        llm.complete_json(
            "translate",
            system=_system_prompt(lang_name, code),
            messages=[
                {"role": "user", "content": json.dumps(batch, ensure_ascii=False, indent=0)}
            ],
            max_tokens=8000,
        )
    )
    # Guard against dropped or renamed keys: fall back to English for any miss.
    return {k: out.get(k, batch[k]) for k in batch}


def _read_catalog(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a JSON object")
    return data


def _write_catalog(path: Path, catalog: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def translate_keys(
    code: str,
    lang_name: str,
    flat_en: dict[str, str],
    keys: Iterable[str],
    translator: Translator,
    *,
    batch_size: int = BATCH_SIZE,
    log: Callable[[str], None] = print,
) -> dict[str, str]:
    keys = list(keys)
    translated: dict[str, str] = {}
    for start in range(0, len(keys), batch_size):
        chunk = {k: flat_en[k] for k in keys[start : start + batch_size]}
        log(f"  [{code}] {start + 1}-{start + len(chunk)} / {len(keys)} …")
        got = translator(lang_name, code, chunk)
        # Only the requested keys, never extras a translator invented.
        translated.update({k: got.get(k, chunk[k]) for k in chunk})
    return translated


def translate_locale(
    code: str,
    lang_name: str,
    flat_en: dict[str, str],
    *,
    only_missing: bool = False,
    prefix: str | None = None,
    placeholders: bool = False,
    translator: Translator | None = None,
    locales_dir: Path = LOCALES_DIR,
    dry_run: bool = False,
    log: Callable[[str], None] = print,
) -> list[str]:
    """Translate one locale; returns the keys translated (or that would be,
    on a dry run). Whole-catalog mode (no flag) replaces the file; scoped
    mode (--only-missing and/or --prefix) merges into the existing file."""
    prefix = normalize_prefix(prefix)
    dest = locales_dir / code / "translation.json"
    scoped = only_missing or placeholders or prefix is not None
    target = _read_catalog(dest) if scoped else None
    flat_target = flatten(target) if target is not None else None
    keys = select_keys(flat_en, flat_target, only_missing=only_missing, prefix=prefix, placeholders=placeholders)
    if dry_run:
        log(f"  [{code}] {len(keys)} key(s) to translate")
        for k in keys[:50]:
            log(f"    {k}")
        if len(keys) > 50:
            log(f"    ... and {len(keys) - 50} more")
        return keys
    if not keys:
        log(f"  [{code}] nothing to translate")
        return keys
    if translator is None:
        translator = translate_batch
    translated = translate_keys(code, lang_name, flat_en, keys, translator, log=log)
    if scoped:
        catalog = merge_translations(target or {}, translated)
    else:
        catalog = unflatten(translated)
    _write_catalog(dest, catalog)
    log(f"  [{code}] wrote {dest} ({len(translated)} key(s))")
    return keys


def arm_sweep(sweep_id: str | None) -> str | None:
    """Attribute this process to an open ledger sweep: EVAL_SWEEP_ID plus
    AI_EVAL_LEDGER_ENFORCE=true, then drop the cached settings so the guard
    sees them. Returns the canonical uuid (None when not given)."""
    if not sweep_id:
        return None
    try:
        canonical = str(uuid.UUID(str(sweep_id).strip()))
    except ValueError as exc:
        raise SystemExit(f"--sweep must be one uuid; got {sweep_id!r}") from exc
    os.environ["EVAL_SWEEP_ID"] = canonical
    os.environ["AI_EVAL_LEDGER_ENFORCE"] = "true"
    from app.core.config import get_settings

    get_settings.cache_clear()
    return canonical


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Translate the frontend i18n catalog.")
    p.add_argument("locales", nargs="*", help=f"target locales (default all: {', '.join(TARGETS)})")
    p.add_argument("--only-missing", action="store_true", help="translate only keys absent from the target locale")
    p.add_argument("--placeholders", action="store_true",
                   help="also translate keys whose target text equals the English text (English placeholders)")
    p.add_argument("--prefix", default=None, help="restrict to keys under this dotted prefix, e.g. aiEstimating.")
    p.add_argument("--sweep", default=None, help="open eval ledger sweep id (sets EVAL_SWEEP_ID, arms the ledger)")
    p.add_argument("--dry-run", action="store_true", help="list the keys per locale; no model call, no write")
    return p


def main(
    argv: list[str],
    *,
    translator: Translator | None = None,
    locales_dir: Path | None = None,
    log: Callable[[str], None] = print,
) -> int:
    args = build_parser().parse_args(argv)
    unknown = [c for c in args.locales if c not in TARGETS]
    if unknown:
        log(f"ERROR: unknown locale(s) {', '.join(unknown)}; targets are {', '.join(TARGETS)}")
        return 2
    requested = list(args.locales) or list(TARGETS)
    root = locales_dir or LOCALES_DIR
    source = root / SOURCE_LOCALE / "translation.json"
    flat_en = flatten(json.loads(source.read_text(encoding="utf-8")))
    scoped = args.only_missing or args.placeholders or normalize_prefix(args.prefix) is not None
    live = not args.dry_run and translator is None

    if live:
        sweep = arm_sweep(args.sweep)
        if scoped and sweep is None and not (os.environ.get("EVAL_SWEEP_ID") or "").strip():
            log("ERROR: a scoped live translation runs inside a ledger sweep: pass --sweep <open sweep id> "
                "(scripts/eval_ledger.py open ...), or --dry-run to preview.")
            return 1
        from app.core.config import get_settings
        from app.services import llm

        settings = get_settings()
        if not llm.is_configured("translate", settings):
            log(
                "ERROR: no model configured for translation: set the provider key, "
                "or the SELF_HOSTED_* vars when FULL_SELF_HOSTED_LLMS_ENABLED=true."
            )
            return 1
        log(f"Model: {llm.active_model('translate', settings)}")
    elif args.sweep:
        arm_sweep(args.sweep)

    log(f"Source: {len(flat_en)} strings from {source.name}")
    total = 0
    for code in requested:
        log(f"Translating to {code} ({TARGETS[code]})")
        keys = translate_locale(
            code,
            TARGETS[code],
            flat_en,
            only_missing=args.only_missing,
            prefix=args.prefix,
            placeholders=args.placeholders,
            translator=translator,
            locales_dir=root,
            dry_run=args.dry_run,
            log=log,
        )
        total += len(keys)
    log(f"Done: {total} key(s) {'listed' if args.dry_run else 'translated'}. "
        "Review ceb (and spot-check ur) with a native speaker before relying on them.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
