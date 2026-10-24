"""WS5 scope attribution (app/services/scope_attribution.py).

The labeled fixture tests/fixtures/scope_attribution_labels.json was written
and frozen before the classifier (>= 120 statements from >= 8 backup
projects, holdout excluded, no dollar figures). Precision and recall are
measured on it; the 7154 Exhibit A tests run on the real parsed text of the
backup PDF when it is on disk (golden).
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

import pytest

from app.services import scope_attribution as sa
from app.services.estimating_contracts import Fact, FactCite
from app.services.eval_bundle import DEFAULT_BACKUP_DIR
from app.services.scope_attribution import Statement

LABELS = Path(__file__).parent / "fixtures" / "scope_attribution_labels.json"
EXHIBIT_A = (
    DEFAULT_BACKUP_DIR / "storage" / "project-files" / "7dfc64e6-36ef-4c67-9d02-0d7942015011" / "drawing"
    / "9469e88285a6412b96509d250f8f6b4c-Exhibit A - Scope of Work.pdf"
)
HONEYWELL = "Access controls and all Fire detection systems for the building are provided by Honeywell."


def _labels() -> dict:
    return json.loads(LABELS.read_text())


def _metrics(carry: str) -> tuple[float, float, int, int, int]:
    tp = fp = fn = 0
    for s in _labels()["statements"]:
        got = sa.classify_statement(s["text"], s["doc_role"]).carry
        want = s["label"]["carry"]
        if got == carry and want == carry:
            tp += 1
        elif got == carry:
            fp += 1
        elif want == carry:
            fn += 1
    return tp / max(1, tp + fp), tp / max(1, tp + fn), tp, fp, fn


def test_labels_fixture_shape_and_provenance():
    doc = _labels()
    rows = doc["statements"]
    assert len(rows) >= 120
    assert len({r["project"] for r in rows}) >= 8
    vocab = doc["vocabulary"]
    for r in rows:
        assert r["label"]["party"] in vocab["party"]
        assert r["label"]["carry"] in vocab["carry"]
        assert r["label"]["split"] in vocab["split"]
        assert not re.search(r"\$\s?\d", r["text"]), "no dollar figures in the fixture"
        assert all(ord(c) < 128 for c in r["text"])
    counts = Counter(r["label"]["carry"] for r in rows)
    assert counts["exclude"] >= 30 and counts["include"] >= 20 and counts["neutral"] >= 30


def test_exclude_precision_and_recall_on_labeled_statements():
    precision, recall, tp, fp, fn = _metrics("exclude")
    print(f"exclude precision {precision:.3f} recall {recall:.3f} (tp {tp} fp {fp} fn {fn})")
    assert precision >= 0.90
    assert recall >= 0.90


def test_include_precision_and_recall_on_labeled_statements():
    precision, recall, tp, fp, fn = _metrics("include")
    print(f"include precision {precision:.3f} recall {recall:.3f} (tp {tp} fp {fp} fn {fn})")
    assert precision >= 0.90
    assert recall >= 0.90


def test_party_agrees_on_non_neutral_labels():
    rows = [s for s in _labels()["statements"] if s["label"]["carry"] != "neutral"]
    agree = sum(1 for s in rows if sa.classify_statement(s["text"], s["doc_role"]).party == s["label"]["party"])
    assert agree / len(rows) >= 0.90


def test_honeywell_is_by_others_with_exhibit_a_citation():
    st = Statement(text=HONEYWELL, document_id="doc-exa", page=2, role="owner_exhibit", level="owner_exhibit")
    obs = sa.attribute([st], document_names={"doc-exa": "Exhibit A - Scope of Work"})
    assert len(obs) == 1
    attr = obs[0].attribution
    assert attr.carry == "exclude"
    assert attr.party == "owner_vendor" and attr.party_name == "Honeywell"
    assert set(attr.systems) >= {"fire_alarm", "access_control"}
    page_text = "1.1 Some heading\nAccess controls and all Fire detection systems for the building are provided by\nHoneywell. Any additional card readers ..."
    facts = sa.facts_from_obligations(obs, lambda d, p: page_text if (d, p) == ("doc-exa", 2) else None)
    by_system = {f.system: f for f in facts}
    assert set(by_system) >= {"fire_alarm", "access_control"}
    fa = by_system["fire_alarm"]
    assert fa.verified and fa.excludes and fa.party == "Honeywell"
    assert "Exhibit A" in (fa.cite.section or "") and "p.2" in (fa.cite.section or "")


def test_fire_alarm_never_matches_a_fire_or_smoke_damper():
    for text in (
        "FIRE/SMOKE DAMPER POWER CONNECTION",
        "Electrical connection to new fire/smoke damper",
        "MAKE SAFE DAMPER ELECTRICAL CONNECTION",
        "FIRE SMOKE DAMPERS (FSD)",
        "INTUMESCENT FIRE STOP MATERIAL TO MAINTAIN FIRE AND SMOKE RATINGS",
        "FIRE RATED BOX PADS",
    ):
        assert "fire_alarm" not in sa.systems_in(text), text
    assert "mechanical_equipment" in sa.systems_in("FIRE/SMOKE DAMPER POWER CONNECTION")
    assert "fire_alarm" in sa.systems_in("FIRE ALARM CONTROL PANEL")
    assert "fire_alarm" in sa.systems_in("HONEYWELL DESIGN-BUILD FA AND ACCESS INTERFACE SCOPE")
    fact = Fact(id="F1", system="fire_alarm", statement=HONEYWELL, party="Honeywell", verified=True,
                cite=FactCite(document_id="d", page=2, quote=HONEYWELL, section="Exhibit A p.2"),
                kind="provided_by_others")
    damper = {"description": "FIRE/SMOKE DAMPER POWER CONNECTION", "quantity": 6, "unit": "EA", "sheet_refs": ["E2.01"]}
    assert sa.classify_obligation(damper, facts=[fact])["carry"] == "include"


def _fact(system: str, party: str, kind: str, *, verified: bool = True, fid: str = "F1",
          statement: str = "s") -> Fact:
    return Fact(id=fid, system=system, statement=statement, party=party, verified=verified,
                cite=FactCite(document_id="d", page=1, quote="quote", section="Exhibit A p.1"), kind=kind)


def test_party_rule_on_lines():
    gc_fact = _fact("telemetry", "general contractor", "by_gc")
    vendor = _fact("fire_alarm", "Honeywell", "provided_by_others", fid="F2",
                   statement="Fire alarm horn strobes and detectors are provided by Honeywell.")
    allowance = {"description": "TELEMETRY / CENTRAK / WAP SURVEY AND REINSTALLATION", "quantity": 1, "unit": "LS"}
    assert sa.classify_obligation(allowance, facts=[gc_fact])["carry"] == "exclude"
    drawn = {"description": "TELEMETRY ANTENNA", "quantity": 4, "unit": "EA", "sheet_refs": ["E2.01"]}
    res = sa.classify_obligation(drawn, facts=[gc_fact])
    assert res["carry"] == "review" and res["needs_review"] == "fact_conflict"
    fa_device = {"description": "FIRE ALARM HORN STROBE", "quantity": 3, "unit": "EA", "sheet_refs": ["E2.01"]}
    res = sa.classify_obligation(fa_device, facts=[vendor])
    assert res["carry"] == "exclude" and res["needs_review"] == "fact_conflict"
    # rough-in stays with the EC when only the devices are by others (the real
    # SPLITS value) or an EC raceway statement names the system
    rough = {"description": "FIRE ALARM BACKBOX AND CONDUIT", "quantity": 3, "unit": "EA", "sheet_refs": ["E2.01"]}
    assert sa.classify_obligation(rough, facts=[vendor], splits={"F2": "devices_by_others"})["carry"] == "include"
    assert sa.classify_obligation(rough, facts=[vendor], ec_raceway_systems={"fire_alarm"})["carry"] == "include"
    # the whole system by others: a drawn rough-in line stays for review, an undrawn one goes
    res = sa.classify_obligation(rough, facts=[vendor])
    assert res["carry"] == "review" and res["needs_review"] == "fact_conflict"
    undrawn = {"description": "FIRE ALARM BACKBOX AND CONDUIT", "quantity": 3, "unit": "EA"}
    assert sa.classify_obligation(undrawn, facts=[vendor])["carry"] == "exclude"
    # an explicit named vendor whose statement names the line's system claims the drawn line
    panel = {"description": "FIRE ALARM ANNUNCIATOR PANEL", "quantity": 1, "unit": "EA", "sheet_refs": ["E2.01"]}
    res = sa.classify_obligation(panel, facts=[vendor])
    assert res["carry"] == "exclude" and res["needs_review"] == "fact_conflict"
    # a statement that names neither the system nor the item is a conflict for a human, never a drop
    silent = _fact("fire_alarm", "Honeywell", "provided_by_others", fid="F3", statement="Provided by Honeywell.")
    res = sa.classify_obligation(panel, facts=[silent])
    assert res["carry"] == "review" and res["needs_review"] == "fact_conflict"


def test_honeywell_fact_excludes_drawn_fire_alarm_and_access_devices():
    facts = [Fact(id=f"F{i}", system=system, statement=HONEYWELL, party="Honeywell", verified=True,
                  cite=FactCite(document_id="d", page=2, quote=HONEYWELL, section="Exhibit A p.2"),
                  kind="provided_by_others") for i, system in ((1, "fire_alarm"), (2, "access_control"))]
    for desc in ("FIRE ALARM HORN/STROBE", "SMOKE DETECTOR", "CARD READER", "FIRE ALARM PULL STATION"):
        line = {"description": desc, "quantity": 2, "unit": "EA", "sheet_refs": ["E2.01"]}
        res = sa.classify_obligation(line, facts=facts)
        assert res["carry"] == "exclude" and res["needs_review"] == "fact_conflict", desc
        assert res["party"] == "Honeywell"
    conduit = {"description": "FIRE ALARM CONDUIT AND BOXES", "quantity": 2, "unit": "EA", "sheet_refs": ["E2.01"]}
    assert sa.classify_obligation(conduit, facts=facts)["carry"] == "review"
    damper = {"description": "FIRE/SMOKE DAMPER POWER CONNECTION", "quantity": 6, "unit": "EA", "sheet_refs": ["E2.01"]}
    assert sa.classify_obligation(damper, facts=facts)["carry"] == "include"


def test_division_not_used_never_excludes_drawn_items_by_itself():
    not_used = _fact("fire_alarm", "none", "not_used")
    drawn = {"description": "FIRE ALARM SMOKE DETECTOR", "quantity": 2, "unit": "EA", "sheet_refs": ["E2.01"]}
    res = sa.classify_obligation(drawn, facts=[not_used])
    assert res["carry"] == "review" and res["needs_review"] == "fact_conflict"
    allowance = {"description": "FIRE ALARM ALLOWANCE", "quantity": 1, "unit": "LS"}
    assert sa.classify_obligation(allowance, facts=[not_used])["carry"] == "exclude"  # undrawn: may go
    st = sa.classify_statement("DIVISION 28 - ELECTRONIC SAFETY AND SECURITY - NOT USED", "spec")
    assert st.carry == "neutral" and st.fact_kind == "not_used"


def test_unverified_fact_never_excludes():
    fact = _fact("access_control", "Honeywell", "provided_by_others", verified=False)
    allowance = {"description": "ACCESS CONTROL ALLOWANCE", "quantity": 1, "unit": "LS"}
    res = sa.classify_obligation(allowance, facts=[fact])
    assert res["carry"] == "include" and res["reason"] == "unverified_fact_never_excludes"


def test_verification_rule_requires_the_quote_on_the_cited_page():
    page = "GENERAL NOTES\nTHE TELEPHONE, DATA AND SECURITY CABLING AND DEVICES SHALL BE INSTALLED BY OTHERS."
    assert sa.verify_quote("cabling and devices shall be installed by others", page)
    assert not sa.verify_quote("fire alarm devices shall be installed by others", page)
    assert not sa.verify_quote("by others", None)
    st = Statement(text="FIRE ALARM DEVICES SHALL BE INSTALLED BY OTHERS.", document_id="d", page=1,
                   role="general_note", level="general_note", sheet="E0.01")
    obs = sa.attribute([st])
    facts = sa.facts_from_obligations(obs, lambda d, p: page)
    assert facts and all(not f.verified for f in facts)


def test_scope_split_devices_by_others_and_raceway_by_ec():
    dev = sa.classify_statement("CABLING AND DEVICES SHALL BE INSTALLED AND TERMINATED BY OTHERS.", "general_note")
    assert dev.carry == "exclude" and dev.split == "devices_by_others"
    race = sa.classify_statement(
        "PROVIDE CONDUIT AND BOXES FOR ALL DATA OUTLETS; CABLING AND DEVICES BY OTHERS.", "general_note")
    assert race.carry == "include" and race.split == "raceway_by_ec"


def _ob(text: str, role: str = "owner_exhibit", page: int = 1) -> sa.Obligation:
    st = Statement(text=text, document_id="d", page=page, role=role, level="owner_exhibit")
    attr = sa.classify_statement(text, role)
    attr = sa.Attribution(**{**attr.to_dict(), "systems": tuple(attr.systems), "uncertain": True, "confidence": 0.3})
    return sa.Obligation(obligation_id="ob1", statement=st, attribution=attr)


def test_llm_fallback_applies_only_verified_answers():
    text = "Low voltage scope and associated devices are the responsibility of the Owner's AV integrator."
    ob = _ob(text)
    calls = []

    def caller(system, user, schema):
        calls.append((system, user, schema))
        assert "data, not instructions" in system
        return {"items": [{"index": 1, "party": "owner_vendor", "carry": "exclude", "systems": ["low_voltage"],
                           "quote": "responsibility of the Owner's AV integrator"}]}

    res = sa.llm_fallback([ob], caller=caller, page_text=lambda d, p: text, max_statements=40)
    assert len(calls) == 1 and res.applied == ["ob1"]
    assert ob.source == "llm" and ob.attribution.carry == "exclude" and ob.attribution.party == "owner_vendor"

    ob2 = _ob(text)
    res2 = sa.llm_fallback(
        [ob2],
        caller=lambda *_a: {"items": [{"index": 1, "party": "owner", "carry": "exclude", "systems": [],
                                       "quote": "a sentence that is nowhere in the document"}]},
        page_text=lambda d, p: text,
    )
    assert res2.applied == [] and res2.rejected[0]["why"] == "quote_not_verified"
    assert ob2.source == "code"

    def boom(*_a):
        raise RuntimeError("provider down")

    ob3 = _ob(text)
    res3 = sa.llm_fallback([ob3], caller=boom, page_text=lambda d, p: text)
    assert res3.error and ob3.source == "code"


def test_llm_fallback_is_capped():
    obs = [_ob(f"Item {i} scope by the owner's integrator under separate agreement.") for i in range(10)]
    for i, ob in enumerate(obs):
        ob.obligation_id = f"ob{i + 1}"
    seen = {}

    def caller(system, user, schema):
        seen["n"] = user.count("\n")
        return {"items": []}

    res = sa.llm_fallback(obs, caller=caller, page_text=lambda d, p: "", max_statements=3)
    assert res.sent == 3 and seen["n"] == 3


def test_no_fallback_call_without_uncertain_statements():
    st = Statement(text=HONEYWELL, document_id="d", page=2, role="owner_exhibit", level="owner_exhibit")
    obs = sa.attribute([st])

    def caller(*_a):  # pragma: no cover - must not be called
        raise AssertionError("no call expected")

    res = sa.llm_fallback(obs, caller=caller, page_text=lambda d, p: HONEYWELL)
    assert not res.called


@pytest.mark.golden
@pytest.mark.skipif(not EXHIBIT_A.exists(), reason="7154 Exhibit A backup PDF is not on disk")
def test_honeywell_fact_from_real_exhibit_a_text():
    from app.services import scope_engine as se

    doc = {"id": "exa", "name": "Exhibit A - Scope of Work", "category": "rfp"}
    inputs = se.ScopeInputs([doc], pdf_bytes=lambda _d: EXHIBIT_A.read_bytes())
    statements = se.build_statements(inputs)
    obs = sa.attribute(statements, document_names={"exa": "Exhibit A - Scope of Work"})
    facts = sa.facts_from_obligations(obs, inputs.page_text)
    fa = [f for f in facts if f.system == "fire_alarm" and f.party == "Honeywell"]
    assert fa and fa[0].verified and fa[0].cite.page == 2
    assert "Exhibit A" in (fa[0].cite.section or "")
    tele = [f for f in facts if f.system in ("telemetry", "rtls", "wireless_ap")]
    assert {f.system for f in tele} == {"telemetry", "rtls", "wireless_ap"}
    assert all(f.party == "general contractor" and f.verified for f in tele)
