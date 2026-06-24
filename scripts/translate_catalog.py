#!/usr/bin/env python
"""One-time (re-runnable) translator for the frontend i18n catalog.

Reads the English source catalog (bdr_fe/locales/en/translation.json) and writes
a translated catalog for each supported non-English locale, reusing the Anthropic
client already configured for the app (no new dependency, no infra). Output is
committed to the repo — there is NO runtime translation.

Usage (from bdr_be/, with ANTHROPIC_API_KEY in the environment / .env):
    python scripts/translate_catalog.py            # all target locales
    python scripts/translate_catalog.py hi ur      # only the given locales

Notes:
- Interpolation placeholders ({{name}}) and brand/technical tokens (G3, BDR, RFQ,
  BOQ, PDF, PM/PE/PA) are preserved verbatim.
- Cebuano (ceb) is the weakest-supported language for every engine, and Urdu (ur)
  is RTL — both should get a native-speaker review pass before being relied on.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# Make `app` importable when run as a plain script from bdr_be/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import get_settings  # noqa: E402

# bdr_be/scripts/ -> repo root -> bdr_fe/locales
LOCALES_DIR = Path(__file__).resolve().parent.parent.parent / "bdr_fe" / "locales"
SOURCE_FILE = LOCALES_DIR / "en" / "translation.json"

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


def _parse_json(text: str) -> dict[str, str]:
    """Tolerantly parse the model's JSON (strip ``` fences if present)."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        if t.endswith("```"):
            t = t[:-3]
        if t.lstrip().startswith("json"):
            t = t.lstrip()[4:]
    data = json.loads(t)
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
        "- Keep interpolation placeholders EXACTLY as written, e.g. {{name}}, {{count}} "
        "— never translate or reorder their inner text.\n"
        f"- Do NOT translate these tokens, keep them verbatim: {', '.join(DO_NOT_TRANSLATE)}.\n"
        "- Preserve leading/trailing whitespace, ellipses (…), and punctuation.\n"
        "- Keep the meaning faithful; do not add or drop information."
    )


def translate_batch(client, model: str, lang_name: str, code: str, batch: dict[str, str]) -> dict[str, str]:
    resp = client.messages.create(
        model=model,
        max_tokens=8000,
        system=[{"type": "text", "text": _system_prompt(lang_name, code), "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": json.dumps(batch, ensure_ascii=False, indent=0)}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    out = _parse_json(text)
    # Guard against dropped/renamed keys — fall back to English for any miss.
    return {k: out.get(k, batch[k]) for k in batch}


def translate_locale(client, model: str, code: str, lang_name: str, flat_en: dict[str, str]) -> None:
    keys = list(flat_en)
    translated: dict[str, str] = {}
    for start in range(0, len(keys), BATCH_SIZE):
        chunk = {k: flat_en[k] for k in keys[start : start + BATCH_SIZE]}
        print(f"  [{code}] {start + 1}-{start + len(chunk)} / {len(keys)} …", flush=True)
        translated.update(translate_batch(client, model, lang_name, code, chunk))

    dest = LOCALES_DIR / code / "translation.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(unflatten(translated), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"  [{code}] wrote {dest.relative_to(LOCALES_DIR.parent.parent)}", flush=True)


def main(argv: list[str]) -> int:
    from anthropic import Anthropic

    settings = get_settings()
    if not settings.anthropic_api_key:
        print("ERROR: anthropic_api_key is not set (env / .env).", file=sys.stderr)
        return 1

    requested = [c for c in argv if c in TARGETS] or list(TARGETS)
    flat_en = flatten(json.loads(SOURCE_FILE.read_text(encoding="utf-8")))
    print(f"Source: {len(flat_en)} strings from {SOURCE_FILE.name}")

    client = Anthropic(api_key=settings.anthropic_api_key)
    model = settings.claude_boq_model  # strongest configured model (opus) — best for low-resource langs
    for code in requested:
        print(f"Translating → {code} ({TARGETS[code]}) with {model}")
        translate_locale(client, model, code, TARGETS[code], flat_en)

    print("Done. Review ceb (and spot-check ur) with a native speaker before relying on them.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
