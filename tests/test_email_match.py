"""Deterministic subject matching (services/email_match) — pure, no I/O.

R2's contract: assign ONLY on a single unambiguous hit. These tests pin the
guardrails that keep false auto-assignments out of project email lists.
"""

from app.services.email_match import normalize, prefilter_candidates, r2_match

P_RIVERSIDE = {"id": "p1", "number": "26-104", "name": "Riverside Plaza"}
P_MAPLE = {"id": "p2", "number": "26-999", "name": "Maple Street TI"}
P_SHORT = {"id": "p3", "number": "21", "name": "Main"}
ALL = [P_RIVERSIDE, P_MAPLE, P_SHORT]


def test_normalize_lowers_and_collapses_punctuation():
    assert normalize("RE: 26-104 – Riverside  Plaza!") == "re 26 104 riverside plaza"


# ── Job-number matching ────────────────────────────────────────────────────────


def test_number_matches_with_punctuation_variants():
    assert r2_match("RE: 26-104 - Riverside BOM", ALL) == "p1"
    assert r2_match("Fwd: 26.104 close-out", ALL) == "p1"


def test_number_matches_hyphen_stripped_variant():
    assert r2_match("Invoice for 26104", ALL) == "p1"


def test_number_never_matches_inside_a_longer_number():
    # "26-104" must not hit "26-1040" (nor the squashed "261040").
    assert r2_match("RE: 26-1040 punch list", ALL) is None
    assert r2_match("job 261040 update", ALL) is None


def test_short_number_can_never_match():
    # Legacy number "21" (< 4 alnum chars) must not hit "21st Street".
    assert r2_match("21st Street bid invite", ALL) is None
    assert r2_match("job 21 update", ALL) is None


# ── Project-name matching ──────────────────────────────────────────────────────


def test_name_matches_whole_phrase():
    assert r2_match("Fwd: maple street ti — revised schedule", ALL) == "p2"


def test_name_requires_phrase_boundaries():
    # "maple streets" is not "maple street ti"; partial names don't hit.
    assert r2_match("maple streets development", ALL) is None


def test_generic_short_name_is_rejected():
    # "Main" fails the length guard even when it appears verbatim.
    assert r2_match("Main breaker replacement", ALL) is None


# ── Disambiguation ─────────────────────────────────────────────────────────────


def test_two_number_hits_is_ambiguous():
    both = [P_RIVERSIDE, {"id": "p4", "number": "26-105", "name": "Other"}]
    assert r2_match("RE: 26-104 and 26-105 combined bid", both) is None


def test_number_hit_outranks_name_hit():
    # Subject names p2's project AND p1's number → the number wins.
    assert r2_match("26-104: maple street ti coordination", ALL) == "p1"


def test_no_hits_returns_none():
    assert r2_match("Lunch on Friday?", ALL) is None
    assert r2_match("", ALL) is None


# ── Spec-section / year lookalike guards ──────────────────────────────────────


def test_spec_sections_never_impersonate_job_numbers():
    # CSI Division 26 (electrical) sections collide head-on with 26-xxxx job
    # numbers when multi-joiner tokens get collapsed — they must not match.
    projects = [{"id": "p9", "number": "26-0519", "name": "Grounding Upgrade"}]
    assert r2_match("RFI: spec section 26.05.19 grounding conductors", projects) is None
    projects = [{"id": "p10", "number": "26-104", "name": "Riverside Plaza"}]
    assert r2_match("Section 2.6.104 review", projects) is None
    # ...while the real single-joiner forms still hit.
    assert r2_match("RE: 26-104 punch", projects) == "p10"
    assert r2_match("RE: 26104 punch", projects) == "p10"


def test_bare_year_numbers_are_unmatchable():
    projects = [
        {"id": "py1", "number": "20-26", "name": "Legacy Warehouse"},
        {"id": "py2", "number": "2026", "name": "Other Job"},
    ]
    assert r2_match("Fwd: 2026 holiday schedule", projects) is None
    assert r2_match("Q3 2026 forecast", projects) is None
    # The name path still works for such projects.
    assert r2_match("Legacy Warehouse gear delivery", projects) == "py1"


def test_fullwidth_digits_normalize_instead_of_vanishing():
    projects = [{"id": "p1", "number": "26-104", "name": "Riverside Plaza"}]
    # NFKC folds '２' to '2': the subject reads 26-1042, which must NOT match
    # 26-104 (previously the exotic digit became a boundary and it did).
    assert r2_match("RE: 26-104２ close-out", projects) is None


# ── R3 candidate prefilter ─────────────────────────────────────────────────────


def test_prefilter_noop_when_under_cap():
    assert prefilter_candidates("anything", ALL, 10) == ALL


def test_prefilter_ranks_subject_affinity_first_and_respects_cap():
    kept = prefilter_candidates("RE: 26-104 Riverside Plaza punch", ALL, 2)
    assert len(kept) == 2
    assert kept[0]["id"] == "p1"


def test_prefilter_is_deterministic():
    a = prefilter_candidates("maple", ALL, 2)
    b = prefilter_candidates("maple", ALL, 2)
    assert a == b


def test_prefilter_force_includes_actual_number_hits():
    # A squashed-number subject shares zero normalized tokens with its project
    # label ("26104" vs "26 104") — the hit logic, not difflib, must keep the
    # true candidates inside a small top-N over a big book.
    book = [
        {"id": f"noise-{i}", "number": f"99-{i:03d}", "name": f"Filler Job {i}"}
        for i in range(200)
    ]
    book += [
        {"id": "t1", "number": "26-104", "name": "Riverside Plaza"},
        {"id": "t2", "number": "26-105", "name": "Lakeside Annex"},
    ]
    kept = prefilter_candidates("Fwd: 26104 26105 combined bid", book, 25)
    ids = {p["id"] for p in kept}
    assert {"t1", "t2"} <= ids
    assert len(kept) == 25
