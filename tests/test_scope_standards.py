"""WS5 standards resolver (app/services/scope_standards.py): precedence,
neutral code references, raceway conflicts to the house value with
needs_review, demolition keynote over sheet note (E20)."""

from __future__ import annotations


import pytest

from app.services import scope_standards as ss
from app.services.eval_bundle import DEFAULT_BACKUP_DIR
from app.services.scope_attribution import Statement
from app.services.scope_policy_seed import SEED_POLICY

ELEC = (
    DEFAULT_BACKUP_DIR / "storage" / "project-files" / "7dfc64e6-36ef-4c67-9d02-0d7942015011" / "drawing"
    / "c85a4720840d49a288d9d0c7d3c87fed-Reference Material - Architectural - ELEC_RESPONSE_TO_CLV_COMMENTS D1.pdf"
)


def st(text: str, level: str, *, sheet: str | None = None, ref: str | None = None, role: str | None = None,
       page: int = 1) -> Statement:
    return Statement(text=text, document_id="doc", page=page, role=role or level, level=level, sheet=sheet, ref=ref)


def test_rgs_required_spec_plus_nec_358_sheet_note_is_rgs_never_emt_silently():
    statements = [
        st("All branch circuit raceways shall be rigid galvanized steel conduit (RGS).", "spec", role="electrical_spec"),
        st("FOR CONDUCTORS WITHIN PVC FOLLOW NEC 352, AND EMT FOLLOW NEC 358.", "sheet_note", sheet="E2.01", ref="SN8"),
    ]
    d = ss.resolve_raceway(statements, SEED_POLICY)
    assert d.value == "RGS" or d.needs_review == ss.RACEWAY_REVIEW
    assert not (d.value == "EMT" and d.needs_review is None)
    assert d.value == "RGS" and d.source == "documents"
    assert any("NEC 358" in n for n in d.neutral_refs)


def test_code_reference_alone_is_neutral_and_the_house_value_stands():
    d = ss.resolve_raceway([st("EMT FOLLOW NEC 358.", "sheet_note", sheet="E2.01")], SEED_POLICY)
    assert d.value == "EMT" and d.source == "house" and d.needs_review is None
    assert d.neutral_refs


def test_raceway_conflict_resolves_to_house_with_review_and_clarification():
    statements = [
        st("EMT WITH STEEL INSULATED THROAT SET SCREW FITTINGS MAY BE USED IN DRY, PROTECTED INTERIOR LOCATIONS.",
           "general_note", sheet="E0.01", ref="GN5"),
        st("ALL WIRING ABOVE CEILING SHALL BE IN RIGID CONDUIT.", "general_note", sheet="E0.01", ref="GN6"),
    ]
    d = ss.resolve_raceway(statements, SEED_POLICY)
    assert d.value == "EMT" and d.source == "conflict" and d.needs_review == ss.RACEWAY_REVIEW
    assert "EMT" in d.clarification and "E0.01 GN5" in d.clarification and "E0.01 GN6" in d.clarification


def test_emt_prohibited_with_nothing_else_needs_review():
    d = ss.resolve_raceway([st("EMT SHALL NOT BE USED.", "spec")], SEED_POLICY)
    assert d.needs_review == ss.RACEWAY_REVIEW and d.value != "EMT"


def test_below_grade_and_riser_clauses_are_not_branch_raceway():
    statements = [
        st("PVC COATED RIGID CONDUIT SHALL BE USED BELOW GRADE.", "general_note"),
        st("WRAPPED RIGID ELBOWS AND RISERS SHALL BE USED FOR ALL STUB-UPS.", "general_note"),
    ]
    d = ss.resolve_raceway(statements, SEED_POLICY)
    assert d.value == "EMT" and d.source == "house" and d.needs_review is None


def test_mc_prohibition_never_conflicts_with_emt():
    statements = [
        st("EMT MAY BE USED IN DRY INTERIOR LOCATIONS.", "general_note"),
        st("MC CABLE SHALL NOT BE ALLOWED.", "general_note"),
    ]
    d = ss.resolve_raceway(statements, SEED_POLICY)
    assert d.value == "EMT" and d.needs_review is None and d.source == "documents"


def test_demolition_keynote_remove_to_source_beats_sheet_note_maintain_and_cap():
    statements = [
        st("THE CONTRACTOR SHALL REMOVE LIGHT FIXTURES/EQUIPMENT AS INDICATED. MAINTAIN AND CAP OFF EXISTING CONDUIT "
           "AND CONDUCTORS FOR USE IN NEW CONSTRUCTION.", "sheet_note", sheet="ED3.01", ref="SN1"),
        st("EXISTING LIGHT FIXTURE TO BE REMOVED. REMOVAL SHALL INCLUDE BUT NOT BE LIMITED TO REMOVAL OF LIGHT FIXTURE, "
           "CONDUIT, CONDUCTORS, ETC FROM LIGHT FIXTURE TO POINT OF SOURCE.", "keynote", sheet="ED3.01", ref="KN1"),
    ]
    d = ss.resolve_by_precedence(statements, "demo_disposition", SEED_POLICY)
    assert d.value == "REMOVE_TO_SOURCE" and d.source == "precedence" and d.needs_review is None
    assert "ED3.01 KN1" in d.clarification and "ED3.01 SN1" in d.clarification
    assert set(d.cites) >= {"ED3.01 KN1", "ED3.01 SN1"}


def test_demolition_same_level_conflict_needs_review():
    statements = [
        st("MAINTAIN AND CAP EXISTING CONDUIT.", "sheet_note", sheet="ED2.01", ref="SN1"),
        st("REMOVE ALL CONDUIT AND WIRING BACK TO SOURCE.", "sheet_note", sheet="ED3.01", ref="SN1"),
    ]
    d = ss.resolve_by_precedence(statements, "demo_disposition", SEED_POLICY)
    assert d.value == "REMOVE_TO_SOURCE" and d.needs_review == "demo_conflict"


def test_resolve_covers_every_policy_standard():
    out = ss.resolve([], SEED_POLICY)
    assert set(out) == {"raceway", "branch_conduit", "emt_fittings", "conductor_min_size", "demo_disposition"}
    assert out["branch_conduit"].value == '3/4"C EMT'
    assert all(v.to_dict()["decided"] for v in out.values())


@pytest.mark.golden
@pytest.mark.skipif(not ELEC.exists(), reason="7154 electrical set is not in the backup on disk")
def test_7154_real_text_raceway_conflict_and_demolition_keynote():
    from app.services import scope_engine as se

    doc = {"id": "elec", "name": "ELEC", "category": "electrical_drawings"}
    inputs = se.ScopeInputs([doc], pdf_bytes=lambda _d: ELEC.read_bytes())
    statements = se.build_statements(inputs)
    race = ss.resolve_raceway(statements, SEED_POLICY)
    assert race.value == "EMT" and race.needs_review == ss.RACEWAY_REVIEW
    assert any("NEC 358" in n for n in race.neutral_refs)
    demo = ss.resolve_by_precedence(statements, "demo_disposition", SEED_POLICY)
    assert demo.value == "REMOVE_TO_SOURCE" and demo.source == "precedence" and demo.needs_review is None
    assert any(c.startswith("ED3.01 KN") for c in demo.cites) and "ED3.01 SN1" in demo.cites
