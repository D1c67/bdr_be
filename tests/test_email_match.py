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


# ── Fuzzy round 2 (r2_score) ───────────────────────────────────────────────────

from app.core.config import Settings  # noqa: E402
from app.services.email_match import could_match, r2_score  # noqa: E402

_S = Settings(_env_file=None)
FUZZY_BOOK = [
    {"id": "a", "number": "26.8.7180", "name": "Sunrise Elementary School"},
    {"id": "b", "number": "26.9.7201", "name": "Henderson Fire Station No. 7"},
    {"id": "c", "number": "26.9.7202", "name": "Henderson Fire Station No. 12"},
    {"id": "d", "number": "26.10.7267", "name": "Lake Mead Pkwy Apartments Phase 2"},
    {"id": "e", "number": "26.10.7268", "name": "Lake Mead Pkwy Apartments Phase 1"},
]


def test_exact_hits_score_one_and_keep_their_kind():
    r = r2_score("RE: 26.9.7201 meeting", FUZZY_BOOK, _S)
    assert (r.project_id, r.kind, r.similarity) == ("b", "number", 1.0)
    assert r.candidates[0]["project_id"] == "b" and r.candidates[0]["similarity"] == 1.0


def test_fuzzy_abbreviation_assigns_with_its_similarity():
    r = r2_score("RE: Sunrise Elem School - lighting submittal", FUZZY_BOOK, _S)
    assert r.project_id == "a" and r.kind == "fuzzy"
    assert 0.9 <= r.similarity < 1.0


def test_typo_gets_partial_credit():
    r = r2_score("Sunrise Elementery School punch walk", FUZZY_BOOK, _S)
    assert r.project_id == "a" and r.kind == "fuzzy"


def test_discriminator_conflict_caps_the_sibling_and_partial_names_wait_for_r3():
    r = r2_score("Fire Station 12 panel schedule", FUZZY_BOOK, _S)
    assert r.project_id is None  # 3 of 4 name words: below the auto threshold
    by_id = {c["project_id"]: c for c in r.candidates}
    assert by_id["c"]["similarity"] > by_id["b"]["similarity"]
    assert by_id["b"]["conflict"] is not None
    assert by_id["b"]["similarity"] <= _S.rfp_match_conflict_cap


def test_a_short_generic_subject_never_fully_matches_a_longer_name():
    r = r2_score("Apartments phase 1", FUZZY_BOOK, _S)
    assert r.project_id is None
    assert r.candidates[0]["project_id"] == "e" and r.candidates[0]["similarity"] < 0.9


def test_close_runner_up_blocks_a_fuzzy_assignment():
    book = [
        {"id": "x", "number": "26.1.1001", "name": "Summerlin Medical Office"},
        {"id": "y", "number": "26.1.1002", "name": "Summerlin Medical Offices"},
    ]
    r = r2_score("Summerlin Medical Office lighting", book, _S)
    # "Office" is an exact name hit on x; y fuzzes in too, but the exact
    # rule decides first.
    assert r.project_id == "x" and r.kind == "name"
    r = r2_score("Summerlin Med Office lighting", book, _S)
    assert r.project_id is None  # x and y within the gap


def test_unrelated_subject_has_no_candidates():
    r = r2_score("Lunch on friday", FUZZY_BOOK, _S)
    assert r.project_id is None and r.candidates == []


def test_could_match_gates_the_rescan():
    assert could_match("Sunrise Elem School submittal", FUZZY_BOOK[0], _S)
    assert could_match("26.8.7180 schedule", FUZZY_BOOK[0], _S)
    assert not could_match("Fire Station 12 panel", FUZZY_BOOK[1], _S)
