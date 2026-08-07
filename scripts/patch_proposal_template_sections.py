"""One-time template patch: grow the pricing box to the sectioned 7-row layout.

The proposal's green pricing table (built by patch_proposal_template.py and
since re-saved in Word, which is why EVERY cell now carries fill C5E0B3) gains
three section rows between Material and Labor:

    Description | Bid                                   (bold header)
    Material    | <Material Amount>
    Gear and Power Distribution Equipment               <- new
      *Includes Generator/s  (small italic caption,
       second paragraph in the same label cell)
                | <Gear Amount>
    Underground | <Underground Amount>                  <- new
    Low Voltage | <Low Voltage Amount>                  <- new
    Labor       | <Labor Amount>
    TOTAL       | <Total Amount>                        (bold, thick top border)

The existing rows are left byte-identical: this script only INSERTS the three
new rows (matching the asset's actual look: all cells filled green, cantSplit,
keepNext, right-aligned single-run amount cells). At render time
proposal_docx.remove_absent_section_rows deletes the rows of sections that are
not on the project, so the committed asset always carries all seven rows.

Run from bdr_be:  uv run python scripts/patch_proposal_template_sections.py
Output:           app/assets/proposal_template.docx  (review in Word, then commit)

The script refuses to run twice (it aborts if <Gear Amount> is already there).
"""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

from docx import Document
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls, qn

ASSET = Path(__file__).resolve().parents[1] / "app" / "assets" / "proposal_template.docx"

GREEN = "C5E0B3"  # theme accent6, tint 0.6 (matches every existing cell)
LABEL_W = 4306
VALUE_W = 2000

GENERATOR_CAPTION = "*Includes Generator/s"

# (label, amount token, caption or None) in top-to-bottom insertion order.
NEW_ROWS = [
    ("Gear and Power Distribution Equipment", "<Gear Amount>", GENERATOR_CAPTION),
    ("Underground", "<Underground Amount>", None),
    ("Low Voltage", "<Low Voltage Amount>", None),
]

EXPECTED_LABELS = [
    "Description",
    "Material",
    "Gear and Power Distribution Equipment",
    "Underground",
    "Low Voltage",
    "Labor",
    "TOTAL",
]

ALL_TOKENS = [
    "<Material Amount>",
    "<Gear Amount>",
    "<Underground Amount>",
    "<Low Voltage Amount>",
    "<Labor Amount>",
    "<Total Amount>",
]

_SHD = (
    f'<w:shd w:val="clear" w:color="auto" w:fill="{GREEN}" '
    'w:themeFill="accent6" w:themeFillTint="66"/>'
)


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _label_cell_xml(label: str, caption: str | None) -> str:
    # Single-run label paragraph; optional second small italic caption
    # paragraph in the SAME cell (proposal_docx deletes it when the project
    # has no generator).
    caption_xml = ""
    if caption:
        rpr = '<w:rPr><w:i/><w:sz w:val="16"/><w:szCs w:val="16"/></w:rPr>'
        caption_xml = (
            f"<w:p><w:pPr><w:keepNext/>{rpr}</w:pPr>"
            f'<w:r>{rpr}<w:t xml:space="preserve">{_esc(caption)}</w:t></w:r></w:p>'
        )
    return (
        f'<w:tc><w:tcPr><w:tcW w:w="{LABEL_W}" w:type="dxa"/>{_SHD}</w:tcPr>'
        f"<w:p><w:pPr><w:keepNext/></w:pPr>"
        f'<w:r><w:t xml:space="preserve">{_esc(label)}</w:t></w:r></w:p>'
        f"{caption_xml}</w:tc>"
    )


def _value_cell_xml(token: str) -> str:
    return (
        f'<w:tc><w:tcPr><w:tcW w:w="{VALUE_W}" w:type="dxa"/>{_SHD}</w:tcPr>'
        '<w:p><w:pPr><w:keepNext/><w:jc w:val="right"/></w:pPr>'
        f'<w:r><w:t xml:space="preserve">{_esc(token)}</w:t></w:r></w:p></w:tc>'
    )


def _row_xml(label: str, token: str, caption: str | None) -> str:
    return (
        f"<w:tr {nsdecls('w')}><w:trPr><w:cantSplit/></w:trPr>"
        + _label_cell_xml(label, caption)
        + _value_cell_xml(token)
        + "</w:tr>"
    )


def _tr_text(tr) -> str:
    return "".join(t.text or "" for t in tr.iter(qn("w:t")))


def find_pricing_table(doc):
    """The table that carries <Material Amount> (the green pricing box)."""
    hits = [
        tbl
        for tbl in doc.element.body.iter(qn("w:tbl"))
        if "<Material Amount>" in "".join(t.text or "" for t in tbl.iter(qn("w:t")))
    ]
    if len(hits) != 1:
        raise SystemExit(f"Expected exactly 1 pricing table, found {len(hits)}: aborting.")
    return hits[0]


def visible_text(data: bytes) -> str:
    import html
    import re

    chunks = []
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for name in zf.namelist():
            if name.startswith("word/") and name.endswith(".xml"):
                xml = zf.read(name).decode("utf-8", errors="ignore")
                chunks += re.findall(r"<w:t(?: [^>]*)?>(.*?)</w:t>", xml, re.S)
    return html.unescape("".join(chunks))


def self_check(data: bytes) -> None:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()
        assert len(names) == len(set(names)), "duplicate zip entries"
        from lxml import etree

        etree.fromstring(zf.read("word/document.xml"))

    text = visible_text(data)
    missing = [tok for tok in ALL_TOKENS if tok not in text]
    assert not missing, f"amount tokens missing after patch: {missing}"
    assert GENERATOR_CAPTION in text, "generator caption missing after patch"

    doc = Document(io.BytesIO(data))
    tbl = find_pricing_table(doc)
    rows = tbl.findall(qn("w:tr"))
    assert len(rows) == 7, f"expected 7 rows, found {len(rows)}"

    labels = []
    for tr in rows:
        first_p = tr.find(qn("w:tc") + "/" + qn("w:p"))
        labels.append("".join(t.text or "" for t in first_p.iter(qn("w:t"))).strip())
    assert labels == EXPECTED_LABELS, f"row labels wrong: {labels}"

    # Every amount token must be a SINGLE-RUN paragraph so the renderer's
    # plain-text replacement always lands cleanly.
    for p in tbl.iter(qn("w:p")):
        p_text = "".join(t.text or "" for t in p.iter(qn("w:t")))
        if p_text in ALL_TOKENS:
            runs = p.findall(qn("w:r"))
            assert len(runs) == 1, f"token {p_text!r} spans {len(runs)} runs"

    # The TOTAL row still carries its thick top border.
    total_tr = rows[-1]
    tops = [
        b
        for b in total_tr.iter(qn("w:top"))
        if b.getparent().tag == qn("w:tcBorders") and b.get(qn("w:sz")) == "12"
    ]
    assert len(tops) == 2, "TOTAL thick top border lost"


def main() -> None:
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else ASSET
    if not target.exists():
        raise SystemExit(f"Template asset not found: {target}")

    source_bytes = target.read_bytes()
    if "<Gear Amount>" in visible_text(source_bytes):
        raise SystemExit("Template already has <Gear Amount>: nothing to do.")

    doc = Document(io.BytesIO(source_bytes))
    tbl = find_pricing_table(doc)

    material_rows = [tr for tr in tbl.findall(qn("w:tr")) if "<Material Amount>" in _tr_text(tr)]
    if len(material_rows) != 1:
        raise SystemExit(f"Expected exactly 1 Material row, found {len(material_rows)}: aborting.")

    cursor = material_rows[0]
    for label, token, caption in NEW_ROWS:
        tr = parse_xml(_row_xml(label, token, caption))
        cursor.addnext(tr)
        cursor = tr

    out = io.BytesIO()
    doc.save(out)
    data = out.getvalue()
    self_check(data)
    target.write_bytes(data)

    print(f"OK: wrote {target}")
    print("  - pricing box is now 7 rows: " + ", ".join(EXPECTED_LABELS[1:]))
    print("  - gear label cell carries the small italic '*Includes Generator/s' caption")
    print("  -> open it in Word to review before committing.")


if __name__ == "__main__":
    main()
