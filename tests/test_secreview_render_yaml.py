"""render.yaml must never pair ENVIRONMENT=production with a committed project URL."""

from pathlib import Path

RENDER_YAML = Path(__file__).resolve().parent.parent / "render.yaml"
KNOWN_NON_PROD_REFS = ("bpidntbyvoooqvaispup", "qpfuugcwsdfpxvdgwvwx")


def test_render_yaml_has_no_hardcoded_dev_or_staging_supabase_ref():
    text = RENDER_YAML.read_text()
    for ref in KNOWN_NON_PROD_REFS:
        assert ref not in text


def test_render_yaml_supabase_url_is_operator_supplied():
    lines = RENDER_YAML.read_text().splitlines()
    idx = next(i for i, ln in enumerate(lines) if ln.strip() == "- key: SUPABASE_URL")
    assert lines[idx + 1].strip() == "sync: false"


def test_render_yaml_marked_stale():
    assert "STALE" in RENDER_YAML.read_text().splitlines()[0]
