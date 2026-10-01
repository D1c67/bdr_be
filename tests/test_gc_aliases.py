"""External GC aliases (services/gc_aliases, BUILD_CONTRACT.md 3.3): the
resolve order (alias, contact, domain, provisional, none), the provisional
score floor (PROVISIONAL_MIN_SCORE: below it `none`, candidates kept), the
per-bundle alias and name-words caches, name drift, and the alias writes
(confirm upsert, create with the directory twin guard before and after the
insert and its compensation, list with joined names, repoint and delete
with their follow-through onto the invitations) against the shared
in-memory Supabase fake. Settings are a plain namespace."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import gc_aliases as ga
from tests.test_rfp_email_ingest import FakeDB

SETTINGS = SimpleNamespace(rfp_match_gc_auto_threshold=0.85, rfp_match_runner_up_gap=0.1)
SRC = ga.SOURCE_BC


class CountingDB(FakeDB):
    """FakeDB that counts reads per table."""

    def __init__(self, tables=None):
        super().__init__(tables)
        self.reads: dict[str, int] = {}

    def table(self, name):
        q = super().table(name)
        original = q.select

        def select(*a, **k):
            self.reads[name] = self.reads.get(name, 0) + 1
            return original(*a, **k)

        q.select = select
        return q


GCS = [
    {"id": "gc-mon", "name": "Monument Construction"},
    {"id": "gc-sun", "name": "Sunrise Builders Inc"},
    {"id": "gc-ace", "name": "Ace General Contractors"},
    {"id": "gc-zen", "name": "Zenith Group"},
]
CONTACTS = [
    {"id": "c-jane", "gc_id": "gc-mon", "email": "Jane@MonumentCo.com"},
    {"id": "c-bob", "gc_id": "gc-sun", "email": "bob@sunrisebuild.com"},
    {"id": "c-gm1", "gc_id": "gc-ace", "email": "estimator.ace@gmail.com"},
    {"id": "c-sh1", "gc_id": "gc-ace", "email": "a@shared.com"},
    {"id": "c-sh2", "gc_id": "gc-zen", "email": "z@shared.com"},
]


def _bundle(gcs=None, contacts=None):
    return {
        "gcs": list(GCS if gcs is None else gcs),
        "contacts": list(CONTACTS if contacts is None else contacts),
        "projects": [],
    }


def _db(**tables):
    base = {
        "general_contractors": [dict(g) for g in GCS],
        "gc_contacts": [dict(c) for c in CONTACTS],
        "gc_external_aliases": [],
        "profiles": [{"id": "u-it", "full_name": "Ivy Tech"}],
    }
    base.update(tables)
    return CountingDB(base)


def _resolve(db, bundle, *, external_id="bc-co-1", external_name="Some Company", lead_email=None):
    return ga.resolve(
        db, source=SRC, external_id=external_id, external_name=external_name,
        lead_email=lead_email, bundle=bundle, settings=SETTINGS,
    )


def _alias(external_id="bc-co-1", gc_id="gc-mon", name="Monument Constr LLC", **over):
    row = {
        "id": f"al-{external_id}", "source": SRC, "external_id": external_id,
        "external_name": name, "gc_id": gc_id, "confirmed_by": "u-it",
        "confirmed_at": "2026-09-01T00:00:00+00:00",
    }
    row.update(over)
    return row


# ── resolve: alias ───────────────────────────────────────────────────────────


def test_alias_resolves_and_is_loaded_once_per_bundle():
    db = _db(gc_external_aliases=[_alias()])
    bundle = _bundle()
    first = _resolve(db, bundle, external_name="Monument Constr LLC")
    assert first.kind == ga.KIND_ALIAS
    assert (first.gc_id, first.gc_name) == ("gc-mon", "Monument Construction")
    assert "bc-co-1" in bundle[ga.BUNDLE_KEY]
    second = _resolve(db, bundle, external_name="Monument Constr LLC")
    assert second.kind == ga.KIND_ALIAS
    assert db.reads["gc_external_aliases"] == 1


def test_alias_wins_over_contact_and_carries_contact_id_under_that_gc():
    db = _db(gc_external_aliases=[_alias(gc_id="gc-mon")])
    res = _resolve(db, _bundle(), lead_email="JANE@monumentco.com")
    assert res.kind == ga.KIND_ALIAS
    assert res.contact_id == "c-jane"


def test_alias_for_other_source_is_ignored():
    db = _db(gc_external_aliases=[_alias(source="other")])
    res = _resolve(db, _bundle(), external_name="Monument Construction")
    assert res.kind == ga.KIND_PROVISIONAL


def test_alias_skipped_without_external_id():
    db = _db(gc_external_aliases=[_alias()])
    res = _resolve(db, _bundle(), external_id=None, external_name="Monument Construction")
    assert res.kind == ga.KIND_PROVISIONAL
    assert db.reads.get("gc_external_aliases", 0) == 0


def test_alias_name_drift_follows_without_reasking():
    db = _db(gc_external_aliases=[_alias(name="Old Name")])
    bundle = _bundle()
    res = _resolve(db, bundle, external_name="Monument Builders West")
    assert res.kind == ga.KIND_ALIAS
    assert db.tables["gc_external_aliases"][0]["external_name"] == "Monument Builders West"
    assert bundle[ga.BUNDLE_KEY]["bc-co-1"]["external_name"] == "Monument Builders West"
    # A blank name (NDA masked) never wipes the stored spelling.
    _resolve(db, bundle, external_name="")
    assert db.tables["gc_external_aliases"][0]["external_name"] == "Monument Builders West"


def test_alias_gc_outside_bundle_reads_the_name():
    db = _db(
        gc_external_aliases=[_alias(gc_id="gc-new")],
        general_contractors=[{"id": "gc-new", "name": "Brand New GC"}],
    )
    res = _resolve(db, _bundle())
    assert (res.kind, res.gc_name) == (ga.KIND_ALIAS, "Brand New GC")


def test_object_bundle_caches_on_attribute():
    db = _db(gc_external_aliases=[_alias()])
    bundle = SimpleNamespace(gcs=list(GCS), contacts=list(CONTACTS), projects=[])
    assert _resolve(db, bundle).kind == ga.KIND_ALIAS
    assert _resolve(db, bundle).kind == ga.KIND_ALIAS
    assert db.reads["gc_external_aliases"] == 1


# ── resolve: contact and domain ──────────────────────────────────────────────


def test_contact_match_ignores_case_and_whitespace():
    res = _resolve(_db(), _bundle(), lead_email="  JANE@monumentco.COM ")
    assert res.kind == ga.KIND_CONTACT
    assert (res.gc_id, res.contact_id, res.gc_name) == ("gc-mon", "c-jane", "Monument Construction")


def test_contact_match_from_display_form():
    res = _resolve(_db(), _bundle(), lead_email="Jane Doe <Jane@MonumentCo.com>")
    assert (res.kind, res.contact_id) == (ga.KIND_CONTACT, "c-jane")


def test_plus_tag_resolves_to_the_untagged_contact():
    res = _resolve(_db(), _bundle(), lead_email="Jane+Bids@MonumentCo.com")
    assert (res.kind, res.contact_id) == (ga.KIND_CONTACT, "c-jane")


def test_plus_tag_contact_found_even_on_an_ambiguous_domain():
    contacts = CONTACTS + [{"id": "c-sh3", "gc_id": "gc-zen", "email": "pat@shared.com"}]
    res = _resolve(_db(), _bundle(contacts=contacts), lead_email="pat+rfp@shared.com")
    assert (res.kind, res.gc_id, res.contact_id) == (ga.KIND_CONTACT, "gc-zen", "c-sh3")


def test_unknown_address_on_a_domain_one_gc_owns_is_domain():
    res = _resolve(_db(), _bundle(), lead_email="someone.else@sunrisebuild.com")
    assert (res.kind, res.gc_id, res.contact_id) == (ga.KIND_DOMAIN, "gc-sun", None)
    assert res.gc_name == "Sunrise Builders Inc"


def test_plus_tag_without_contact_falls_back_to_domain():
    res = _resolve(_db(), _bundle(), lead_email="nobody+x@sunrisebuild.com")
    assert (res.kind, res.gc_id) == (ga.KIND_DOMAIN, "gc-sun")


def test_public_mailbox_domain_never_resolves_by_domain():
    res = _resolve(
        _db(), _bundle(), external_name="Monument Construction",
        lead_email="different.person@gmail.com",
    )
    assert res.kind == ga.KIND_PROVISIONAL
    assert res.gc_id == "gc-mon"


def test_exact_public_mailbox_contact_still_resolves_as_contact():
    res = _resolve(_db(), _bundle(), lead_email="Estimator.Ace@gmail.com")
    assert (res.kind, res.gc_id, res.contact_id) == (ga.KIND_CONTACT, "gc-ace", "c-gm1")


def test_two_gcs_sharing_a_domain_is_not_domain():
    res = _resolve(_db(), _bundle(), external_name="Zenith", lead_email="new.person@shared.com")
    assert res.kind == ga.KIND_PROVISIONAL
    assert res.gc_id == "gc-zen"


def test_name_path_of_resolve_gc_is_never_accepted():
    # An exact name would clear resolve_gc's 0.85 bar; it still only counts
    # as provisional here.
    res = _resolve(_db(), _bundle(), external_name="Sunrise Builders Inc", lead_email=None)
    assert res.kind == ga.KIND_PROVISIONAL
    assert res.gc_id == "gc-sun"
    assert ga.KIND_PROVISIONAL not in ga.CONFIRMED_KINDS


# ── resolve: provisional and none ────────────────────────────────────────────


def test_provisional_keeps_top_three_in_resolve_gc_shape():
    res = _resolve(_db(), _bundle(), external_name="Monument Construction Co")
    assert res.kind == ga.KIND_PROVISIONAL
    assert len(res.candidates) == 3
    assert res.candidates[0] == {"gc_id": "gc-mon", "name": "Monument Construction", "score": 1.0}
    assert set(res.candidates[0]) == {"gc_id", "name", "score"}
    scores = [c["score"] for c in res.candidates]
    assert scores == sorted(scores, reverse=True)
    assert res.contact_id is None


def test_below_the_floor_is_none_with_the_candidates_kept_ties_broken_by_name():
    # The dev case of 2026-09-28: an unrelated company scored 0.0 against
    # every GC and the alphabetically first one became the provisional GC.
    gcs = [
        {"id": "g-d", "name": "Delta"},
        {"id": "g-b", "name": "Bravo"},
        {"id": "g-c", "name": "Charlie"},
        {"id": "g-a", "name": "Alpha"},
    ]
    res = _resolve(_db(), _bundle(gcs=gcs, contacts=[]), external_name="Qqq Xxx")
    assert (res.kind, res.gc_id, res.gc_name, res.contact_id) == (ga.KIND_NONE, None, None, None)
    # The card still offers the closest names.
    assert [c["name"] for c in res.candidates] == ["Alpha", "Bravo", "Charlie"]
    assert all(c["score"] == 0.0 for c in res.candidates)


def test_floor_is_inclusive_and_a_score_just_under_is_none(monkeypatch):
    gcs = [{"id": "g-x", "name": "Xylo Builders"}]
    for score, kind in ((0.5, ga.KIND_PROVISIONAL), (0.499, ga.KIND_NONE)):
        monkeypatch.setattr(ga, "_score_words", lambda a, b, s=score: s)
        res = _resolve(_db(), _bundle(gcs=gcs, contacts=[]), external_name="Anything Co")
        assert res.kind == kind
        assert res.candidates == [{"gc_id": "g-x", "name": "Xylo Builders", "score": score}]
        assert res.gc_id == ("g-x" if kind == ga.KIND_PROVISIONAL else None)
    assert ga.PROVISIONAL_MIN_SCORE == 0.5


def test_generic_only_external_name_still_ranks_candidates():
    # normalize_gc_name drops every word of "The Builders Group"; the word
    # list does not, so the name still ranks (generic against generic).
    gcs = [
        {"id": "g-bg", "name": "Builders Group"},
        {"id": "g-mon", "name": "Monument Construction"},
    ]
    res = _resolve(_db(), _bundle(gcs=gcs, contacts=[]), external_name="The Builders Group")
    assert res.candidates and res.candidates[0]["gc_id"] == "g-bg"
    assert res.candidates[0]["score"] >= ga.PROVISIONAL_MIN_SCORE
    assert (res.kind, res.gc_id) == (ga.KIND_PROVISIONAL, "g-bg")
    # A name with no words at all still ranks nothing.
    assert _resolve(_db(), _bundle(gcs=gcs, contacts=[]), external_name=" - ").candidates == []


def test_none_when_there_are_no_gcs():
    res = _resolve(_db(), _bundle(gcs=[], contacts=[]), external_name="Monument", lead_email="a@b.com")
    assert res == ga.GcResolve(kind=ga.KIND_NONE, gc_id=None, gc_name=None, contact_id=None, candidates=[])


def test_none_when_masked_name_and_no_email_hit():
    res = _resolve(_db(), _bundle(), external_id=None, external_name=None, lead_email=None)
    assert res.kind == ga.KIND_NONE


def test_confirmed_kinds_carry_candidates_too():
    res = _resolve(_db(), _bundle(), external_name="Sunrise", lead_email="x@sunrisebuild.com")
    assert res.kind == ga.KIND_DOMAIN
    assert res.candidates and res.candidates[0]["gc_id"] == "gc-sun"


# ── provisional_score on real BuildingConnected / GC name pairs ──────────────

# Different companies that rfp_match._similarity scored 0.50 to 1.00 (one
# shared word, or only generic words in common).
_DIFFERENT_COMPANIES = [
    ("United Construction Company", "Builders United"),
    ("AAA FACILITY SERVICES", "OS Construction Services"),
    ("B&H Construction", "CG&B Enterprises, Inc."),
    ("McCarthy Building Companies", "DC Building Group"),
    ("Construction One Inc.", "Eagle One Construction LLC"),
]

# The same company: (name, name, lowest allowed, highest allowed). The
# bands hold the _similarity scores; the branch suffix may only rise.
_SAME_COMPANY = [
    ("Shaw-Lundquist Associates, Inc.", "Shaw-Lundquist", 0.995, 1.0),
    ("Rafael Construction Inc", "Rafael Construction", 0.995, 1.0),
    ("CORE Construction", "CORE Construction West", 0.583, 0.593),
    ("Tre Builders", "Tre Builders - Las Vegas", 0.3995, 1.0),
    ("Metcalf Builders", "Metcalf Builders - Metcalf Reno Office", 0.995, 1.0),
]


@pytest.mark.parametrize(("a", "b"), _DIFFERENT_COMPANIES)
def test_provisional_score_different_companies_stay_weak(a, b):
    assert ga.provisional_score(a, b) < 0.35
    assert ga.provisional_score(b, a) < 0.35


@pytest.mark.parametrize(("a", "b", "low", "high"), _SAME_COMPANY)
def test_provisional_score_same_company_keeps_its_score(a, b, low, high):
    assert low <= ga.provisional_score(a, b) <= high
    assert ga.provisional_score(b, a) == ga.provisional_score(a, b)


def test_provisional_score_single_distinctive_word_stays_high():
    assert ga.provisional_score("Catamount", "Catamount Constructors, Inc.") == 1.0
    gcs = [
        {"id": "g-cat", "name": "Catamount Constructors, Inc."},
        {"id": "g-mon", "name": "Monument Construction"},
        {"id": "g-bu", "name": "Builders United"},
    ]
    res = _resolve(_db(), _bundle(gcs=gcs, contacts=[]), external_name="Catamount")
    assert (res.kind, res.gc_id) == (ga.KIND_PROVISIONAL, "g-cat")
    assert res.candidates[0]["score"] == 1.0


def test_provisional_candidates_use_provisional_score():
    gcs = [
        {"id": "g-bu", "name": "Builders United"},
        {"id": "g-eo", "name": "Eagle One Construction LLC"},
        {"id": "g-dc", "name": "DC Building Group"},
    ]
    res = _resolve(_db(), _bundle(gcs=gcs, contacts=[]), external_name="United Construction Company")
    # Every score is under the floor: no guess, the candidates kept.
    assert (res.kind, res.gc_id) == (ga.KIND_NONE, None)
    assert res.candidates[0] == {"gc_id": "g-bu", "name": "Builders United", "score": 0.3}
    assert all(c["score"] < 0.35 for c in res.candidates)


# ── confirm ──────────────────────────────────────────────────────────────────


def test_confirm_upserts_idempotently_and_follows_name_drift():
    db = _db()
    first = ga.confirm(db, source=SRC, external_id="bc-9", external_name="Acme GC",
                       gc_id="gc-ace", actor_id="u-it")
    assert first["gc_id"] == "gc-ace" and first["confirmed_by"] == "u-it"
    again = ga.confirm(db, source=SRC, external_id="bc-9", external_name="Acme GC",
                       gc_id="gc-ace", actor_id="u-it")
    assert again["id"] == first["id"]
    drift = ga.confirm(db, source=SRC, external_id="bc-9", external_name="ACME General",
                       gc_id="gc-zen", actor_id=None)
    rows = db.tables["gc_external_aliases"]
    assert len(rows) == 1
    assert (rows[0]["external_name"], rows[0]["gc_id"]) == ("ACME General", "gc-zen")
    assert drift["id"] == first["id"]


def test_confirm_same_external_id_other_source_is_a_second_row():
    db = _db()
    ga.confirm(db, source=SRC, external_id="x", external_name="A", gc_id="gc-ace", actor_id=None)
    ga.confirm(db, source="other", external_id="x", external_name="A", gc_id="gc-ace", actor_id=None)
    assert len(db.tables["gc_external_aliases"]) == 2


def test_confirm_refuses_unknown_gc_and_blank_external_id():
    db = _db()
    with pytest.raises(ga.GcNotFound):
        ga.confirm(db, source=SRC, external_id="bc-9", external_name="X", gc_id="nope", actor_id=None)
    with pytest.raises(ValueError):
        ga.confirm(db, source=SRC, external_id="  ", external_name="X", gc_id="gc-ace", actor_id=None)
    assert db.tables["gc_external_aliases"] == []


def test_confirm_then_resolve_on_a_fresh_bundle_is_alias():
    db = _db()
    ga.confirm(db, source=SRC, external_id="bc-9", external_name="Qqq", gc_id="gc-zen", actor_id=None)
    res = _resolve(db, _bundle(), external_id="bc-9", external_name="Qqq")
    assert (res.kind, res.gc_id) == (ga.KIND_ALIAS, "gc-zen")


# ── create_gc_and_confirm ────────────────────────────────────────────────────


def test_create_new_gc_with_contact_and_alias():
    db = _db()
    out = ga.create_gc_and_confirm(
        db, source=SRC, external_id="bc-new", external_name="Fresh Co (BC)",
        name="  Fresh   Company  ", contact_name=None, contact_email=" Lead@Fresh.COM ",
        contact_phone="702-555-0100", actor_id="u-it",
    )
    assert out["reused"] is False
    gc = next(g for g in db.tables["general_contractors"] if g["id"] == out["gc_id"])
    assert gc["name"] == "Fresh Company"
    contact = next(c for c in db.tables["gc_contacts"] if c["id"] == out["contact_id"])
    assert contact == {
        "id": out["contact_id"], "gc_id": out["gc_id"], "name": "Fresh Company",
        "email": "lead@fresh.com", "phone": "702-555-0100",
    }
    alias = db.tables["gc_external_aliases"][0]
    assert (alias["external_id"], alias["gc_id"], alias["external_name"]) == (
        "bc-new", out["gc_id"], "Fresh Co (BC)",
    )
    assert out["alias"]["id"] == alias["id"]


def test_create_reuses_the_directory_twin_never_a_second_gc():
    db = _db()
    before = len(db.tables["general_contractors"])
    out = ga.create_gc_and_confirm(
        db, source=SRC, external_id="bc-mon", external_name="Monument",
        name="monument   CONSTRUCTION", contact_name="Jane Doe", contact_email="JANE@monumentco.com",
        contact_phone=None, actor_id="u-it",
    )
    assert out["reused"] is True
    assert (out["gc_id"], out["gc_name"]) == ("gc-mon", "Monument Construction")
    assert len(db.tables["general_contractors"]) == before
    # The same address under that GC is reused, not duplicated.
    assert out["contact_id"] == "c-jane"
    assert len(db.tables["gc_contacts"]) == len(CONTACTS)
    assert db.tables["gc_external_aliases"][0]["gc_id"] == "gc-mon"


def test_create_reused_gc_adds_a_new_contact_with_its_name():
    db = _db()
    out = ga.create_gc_and_confirm(
        db, source=SRC, external_id="bc-mon", external_name="Monument",
        name="Monument Construction", contact_name="  Pat   Lee ", contact_email="pat@monumentco.com",
        contact_phone="  ", actor_id=None,
    )
    contact = next(c for c in db.tables["gc_contacts"] if c["id"] == out["contact_id"])
    assert (contact["gc_id"], contact["name"], contact["phone"]) == ("gc-mon", "Pat Lee", None)


def test_create_without_email_adds_no_contact_and_falls_back_to_external_name():
    db = _db()
    out = ga.create_gc_and_confirm(
        db, source=SRC, external_id="bc-x", external_name="Outside Name GC",
        name="  ", contact_name="Someone", contact_email=None, contact_phone=None, actor_id=None,
    )
    assert out["contact_id"] is None
    assert out["gc_name"] == "Outside Name GC"
    assert len(db.tables["gc_contacts"]) == len(CONTACTS)


def test_create_refuses_without_any_name():
    with pytest.raises(ValueError):
        ga.create_gc_and_confirm(
            _db(), source=SRC, external_id="bc-x", external_name=None, name=None,
            contact_name=None, contact_email=None, contact_phone=None, actor_id=None,
        )


# ── list / repoint / delete / rename ─────────────────────────────────────────


def test_list_aliases_joins_gc_and_confirmer_names():
    db = _db(gc_external_aliases=[
        _alias("b", gc_id="gc-sun", name="Zulu Name", confirmed_by="u-it"),
        _alias("a", gc_id="gc-mon", name="Alpha Name", confirmed_by=None),
        _alias("c", gc_id="gc-zen", name="Other Source", source="other"),
    ])
    items = ga.list_aliases(db, SRC)
    assert [i["external_id"] for i in items] == ["a", "b"]
    assert items[0]["gc"] == {"id": "gc-mon", "name": "Monument Construction"}
    assert items[0]["confirmed_by"] is None
    assert items[1]["confirmed_by"] == {"id": "u-it", "name": "Ivy Tech"}
    assert set(items[1]) == {
        "id", "source", "external_id", "external_name", "gc", "confirmed_by", "confirmed_at",
    }


def test_list_aliases_empty():
    assert ga.list_aliases(_db(), SRC) == []


def test_repoint_moves_the_alias_and_records_the_actor():
    db = _db(gc_external_aliases=[_alias(confirmed_by=None)])
    row = ga.repoint(db, "al-bc-co-1", "gc-zen", "u-it")
    assert (row["gc_id"], row["confirmed_by"]) == ("gc-zen", "u-it")
    assert _resolve(db, _bundle()).gc_id == "gc-zen"


def test_repoint_refuses_unknown_alias_or_gc():
    db = _db(gc_external_aliases=[_alias()])
    with pytest.raises(ga.AliasNotFound):
        ga.repoint(db, "nope", "gc-zen", None)
    with pytest.raises(ga.GcNotFound):
        ga.repoint(db, "al-bc-co-1", "nope", None)
    assert db.tables["gc_external_aliases"][0]["gc_id"] == "gc-mon"


def test_delete_forgets_the_alias_and_resolve_asks_again():
    db = _db(gc_external_aliases=[_alias()])
    ga.delete(db, "al-bc-co-1")
    assert db.tables["gc_external_aliases"] == []
    assert _resolve(db, _bundle(), external_name="Monument Construction").kind == ga.KIND_PROVISIONAL
    with pytest.raises(ga.AliasNotFound):
        ga.delete(db, "al-bc-co-1")


def test_rename_external_updates_only_the_named_alias_and_ignores_blank():
    db = _db(gc_external_aliases=[_alias("a", name="Old"), _alias("b", name="Keep")])
    ga.rename_external(db, source=SRC, external_id="a", external_name="  New Name ")
    ga.rename_external(db, source=SRC, external_id="b", external_name="   ")
    names = {r["external_id"]: r["external_name"] for r in db.tables["gc_external_aliases"]}
    assert names == {"a": "New Name", "b": "Keep"}


def test_email_helpers():
    assert ga.normalize_email(None) == ""
    assert ga.normalize_email("not an address") == ""
    assert ga.normalize_email("mailto:A@B.com") == "a@b.com"
    assert ga.untagged_email("a+b@c.com") == "a@c.com"
    assert ga.untagged_email("a@c.com") is None
    assert ga.untagged_email("+b@c.com") is None


def test_no_em_or_en_dashes_in_module_or_tests():
    for path in (Path(ga.__file__), Path(__file__)):
        text = path.read_text(encoding="utf-8")
        assert chr(0x2014) not in text and chr(0x2013) not in text, path


# ── the lead-email step asks resolve_gc for no name (no wasted name loop) ────


def test_email_step_passes_no_name_to_resolve_gc(monkeypatch):
    seen = []
    real = ga.rfp_match.resolve_gc

    def spy(email, bundle, settings):
        seen.append(email.get("extracted_gc_name"))
        return real(email, bundle, settings)

    monkeypatch.setattr(ga.rfp_match, "resolve_gc", spy)
    res = _resolve(_db(), _bundle(), external_name="Sunrise Builders Inc", lead_email="x+y@nowhere.example")
    assert seen and all(name is None for name in seen)   # the tagged and the untagged ask
    assert (res.kind, res.gc_id) == (ga.KIND_PROVISIONAL, "gc-sun")


# ── the per-bundle name-words cache ──────────────────────────────────────────

_CACHE_NAMES = [
    "Monument Construction Co", "United Construction Company", "The Builders Group", "Catamount",
    "CORE Construction West", "Tre Builders - Las Vegas", "Qqq Xxx", "J.E. Dunn", "",
]
_CACHE_GCS = [
    {"id": "g1", "name": "Monument Construction"},
    {"id": "g2", "name": "Builders United"},
    {"id": "g3", "name": "Builders Group"},
    {"id": "g4", "name": "Catamount Constructors, Inc."},
    {"id": "g5", "name": "CORE Construction"},
    {"id": "g6", "name": "Tre Builders"},
    {"id": "g7", "name": "JE Dunn Construction"},
    {"id": "g8", "name": None},
]


def test_name_words_cache_leaves_every_score_unchanged():
    bundle = _bundle(gcs=_CACHE_GCS, contacts=[])
    for name in _CACHE_NAMES:
        cached = ga._candidates(name, _CACHE_GCS, bundle)
        uncached = ga._candidates(name, _CACHE_GCS, None)
        assert cached == uncached, name
        for gc in _CACHE_GCS:
            direct = ga.provisional_score(name, gc["name"])
            via_cache = ga._score_words(ga._name_words(name), ga._gc_words(bundle, gc["name"]))
            assert direct == via_cache, (name, gc["name"])
    # Resolve answers the same with a warm cache as with a cold one.
    for name in _CACHE_NAMES:
        warm = _resolve(_db(), bundle, external_name=name)
        cold = _resolve(_db(), _bundle(gcs=_CACHE_GCS, contacts=[]), external_name=name)
        assert warm == cold, name


def test_name_words_cache_normalizes_each_gc_name_once_per_bundle(monkeypatch):
    calls = []
    real = ga._name_words
    monkeypatch.setattr(ga, "_name_words", lambda name: calls.append(name) or real(name))
    bundle = _bundle()
    for _ in range(3):
        _resolve(_db(), bundle, external_name="Monument Construction Co")
    gc_names = [g["name"] for g in GCS]
    assert sorted(n for n in calls if n in gc_names) == sorted(gc_names)   # once each
    assert calls.count("Monument Construction Co") == 3                     # the row's own name each time
    assert set(bundle[ga.WORDS_BUNDLE_KEY]) == set(gc_names)
    # An object bundle caches on an attribute.
    obj = SimpleNamespace(gcs=list(GCS), contacts=list(CONTACTS), projects=[])
    _resolve(_db(), obj, external_name="Zenith")
    assert set(getattr(obj, ga.WORDS_BUNDLE_KEY)) == set(gc_names)


# ── create_gc_and_confirm: shared contact helpers, the race, compensation ────


def test_create_uses_the_rfp_create_contact_helpers(monkeypatch):
    from app.services import rfp_create

    assert not hasattr(ga, "_contact_for")
    calls = []
    real = rfp_create.ensure_lead_contact
    monkeypatch.setattr(rfp_create, "ensure_lead_contact",
                        lambda sb, gc_id, lead: calls.append((gc_id, lead)) or real(sb, gc_id, lead))
    out = ga.create_gc_and_confirm(
        _db(), source=SRC, external_id="bc-mon", external_name="Monument", name="Monument Construction",
        contact_name="Jane", contact_email="Jane@MonumentCo.com", contact_phone=None, actor_id=None,
    )
    assert calls == [("gc-mon", {"first_name": "Jane", "email": "jane@monumentco.com", "phone": None})]
    assert out["contact_id"] == "c-jane"


def _race_db(twin_created_at, **tables):
    """A db where the twin guard's first read misses a GC another request
    is inserting (it appears between our check and our insert)."""
    db = _db(**tables)
    db.defaults = {**db.defaults, "general_contractors": {"created_at": lambda: "2026-09-28T12:00:00+00:00"}}
    return db, {"id": "gc-twin", "name": "fresh   company", "created_at": twin_created_at}


def test_create_race_loser_deletes_its_row_and_reuses_the_older_twin(monkeypatch):
    db, twin = _race_db("2026-09-28T11:59:59+00:00")

    def first_check(sb, table, name):
        db.tables["general_contractors"].append(dict(twin))   # the concurrent insert lands now
        return None

    monkeypatch.setattr(ga.directory, "find_duplicate_company", first_check)
    out = ga.create_gc_and_confirm(
        db, source=SRC, external_id="bc-new", external_name="Fresh", name="Fresh Company",
        contact_name=None, contact_email="lead@fresh.com", contact_phone=None, actor_id=None,
    )
    assert (out["gc_id"], out["gc_name"], out["reused"]) == ("gc-twin", "fresh   company", True)
    fresh = [g for g in db.tables["general_contractors"] if "fresh" in g["name"].lower()]
    assert [g["id"] for g in fresh] == ["gc-twin"]            # our row is gone
    contact = next(c for c in db.tables["gc_contacts"] if c["id"] == out["contact_id"])
    assert contact["gc_id"] == "gc-twin"
    assert db.tables["gc_external_aliases"][0]["gc_id"] == "gc-twin"


def test_create_race_winner_keeps_its_row(monkeypatch):
    db, twin = _race_db("2026-09-28T12:00:01+00:00")          # the other request is newer

    def first_check(sb, table, name):
        db.tables["general_contractors"].append(dict(twin))
        return None

    monkeypatch.setattr(ga.directory, "find_duplicate_company", first_check)
    out = ga.create_gc_and_confirm(
        db, source=SRC, external_id="bc-new", external_name="Fresh", name="Fresh Company",
        contact_name=None, contact_email=None, contact_phone=None, actor_id=None,
    )
    assert out["reused"] is False and out["gc_id"] != "gc-twin"
    ids = {g["id"] for g in db.tables["general_contractors"]}
    assert {out["gc_id"], "gc-twin"} <= ids                  # the loser deletes its own row, not us


def test_create_failure_after_the_insert_takes_back_the_gc_and_contact(monkeypatch):
    db = _db()
    before_gcs = [dict(g) for g in db.tables["general_contractors"]]
    before_contacts = [dict(c) for c in db.tables["gc_contacts"]]

    def boom(*a, **k):
        raise RuntimeError("alias write failed")

    monkeypatch.setattr(ga, "confirm", boom)
    with pytest.raises(RuntimeError):
        ga.create_gc_and_confirm(
            db, source=SRC, external_id="bc-new", external_name="Fresh", name="Fresh Company",
            contact_name="Lee", contact_email="lee@fresh.com", contact_phone=None, actor_id=None,
        )
    assert db.tables["general_contractors"] == before_gcs
    assert db.tables["gc_contacts"] == before_contacts


def test_create_failure_on_the_contact_takes_back_the_gc(monkeypatch):
    from app.services import rfp_create

    db = _db()
    before_gcs = [dict(g) for g in db.tables["general_contractors"]]

    def boom(sb, gc_id, lead):
        raise RuntimeError("contact insert failed")

    monkeypatch.setattr(rfp_create, "ensure_lead_contact", boom)
    with pytest.raises(RuntimeError):
        ga.create_gc_and_confirm(
            db, source=SRC, external_id="bc-new", external_name="Fresh", name="Fresh Company",
            contact_name=None, contact_email="lee@fresh.com", contact_phone=None, actor_id=None,
        )
    assert db.tables["general_contractors"] == before_gcs
    assert db.tables["gc_external_aliases"] == []


def test_create_failure_keeps_a_gc_something_else_already_refers_to(monkeypatch):
    db = _db()

    def boom(sb, *, gc_id, **k):
        # Someone linked the new GC to a project in the meantime.
        db.tables.setdefault("project_gcs", []).append({"id": "pg-1", "project_id": "p-1", "gc_id": gc_id})
        raise RuntimeError("alias write failed")

    monkeypatch.setattr(ga, "confirm", boom)
    with pytest.raises(RuntimeError):
        ga.create_gc_and_confirm(
            db, source=SRC, external_id="bc-new", external_name="Fresh", name="Fresh Company",
            contact_name=None, contact_email=None, contact_phone=None, actor_id=None,
        )
    assert any(g["name"] == "Fresh Company" for g in db.tables["general_contractors"])


def test_create_failure_never_deletes_a_reused_gc_or_its_contacts(monkeypatch):
    db = _db()
    before_contacts = [dict(c) for c in db.tables["gc_contacts"]]
    monkeypatch.setattr(ga, "confirm", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    with pytest.raises(RuntimeError):
        ga.create_gc_and_confirm(
            db, source=SRC, external_id="bc-mon", external_name="Monument", name="Monument Construction",
            contact_name=None, contact_email="jane@monumentco.com", contact_phone=None, actor_id=None,
        )
    assert any(g["id"] == "gc-mon" for g in db.tables["general_contractors"])
    assert db.tables["gc_contacts"] == before_contacts


# ── repoint / delete follow through to the invitations ───────────────────────


def _inv_row(inv_id, **over):
    row = {
        "id": inv_id, "portal": SRC, "gc_external_id": "bc-co-1", "gc_id": "gc-mon", "gc_kind": "alias",
        "status": "done", "flag_reason": None, "created_project_id": None, "match_project_id": None,
        "gc_confirmed_at": "2026-09-01T00:00:00+00:00", "gc_confirmed_by": "u-it", "change_log": [],
        "attempts": 2, "last_error": "x", "next_attempt_at": None, "decided_at_step": "match",
    }
    row.update(over)
    return row


def _prop_db():
    return _db(
        gc_external_aliases=[_alias()],
        rfp_portal_invitations=[
            _inv_row("i-open"),                                                         # no project
            _inv_row("i-parked", status="review_match", flag_reason="match_gc_unresolved"),
            _inv_row("i-created", status="created", created_project_id="p-1"),           # made p-1
            _inv_row("i-exists", status="exists", match_project_id="p-2"),               # merged onto p-2
            _inv_row("i-review", status="review_match", match_project_id="p-9"),         # a candidate only
            _inv_row("i-prov", gc_kind="provisional", gc_confirmed_at=None),             # not via the alias
            _inv_row("i-other", gc_external_id="bc-co-2"),                               # another company
            _inv_row("i-portal", portal="ngem"),                                         # another source
        ],
        projects=[{"id": "p-1", "gc_confirm_pending": False}, {"id": "p-2", "gc_confirm_pending": False},
                  {"id": "p-9", "gc_confirm_pending": False}],
    )


def _invs(db):
    return {r["id"]: r for r in db.tables["rfp_portal_invitations"]}


def _pending(db):
    return {p["id"]: p["gc_confirm_pending"] for p in db.tables["projects"]}


def test_repoint_moves_unlinked_rows_and_reopens_the_card_on_linked_projects():
    db = _prop_db()
    row = ga.repoint(db, "al-bc-co-1", "gc-zen", "u-it")
    assert row["propagated"] == {"updated": 5, "rematched": 0, "flagged_projects": ["p-1", "p-2"]}
    inv = _invs(db)
    # Rows without a project take the new GC (the parked one stays parked).
    for key in ("i-open", "i-parked", "i-review"):
        assert (inv[key]["gc_id"], inv[key]["gc_kind"]) == ("gc-zen", "alias"), key
    assert inv["i-parked"]["status"] == "review_match"
    # Rows on a project keep the project's GC; the project asks again and the
    # row's change log says why.
    for key in ("i-created", "i-exists"):
        assert inv[key]["gc_id"] == "gc-mon"
        (entry,) = inv[key]["change_log"]
        assert (entry["field"], entry["old"], entry["new"]) == ("gc_alias", "gc-mon", "gc-zen")
    assert _pending(db) == {"p-1": True, "p-2": True, "p-9": False}
    # Rows that did not come through this alias are untouched.
    for key in ("i-prov", "i-other", "i-portal"):
        assert inv[key]["gc_id"] == "gc-mon" and inv[key]["change_log"] == [], key


def test_repoint_to_the_same_gc_changes_no_invitation():
    db = _prop_db()
    row = ga.repoint(db, "al-bc-co-1", "gc-mon", "u-it")
    assert row["propagated"] == {"updated": 0, "rematched": 0, "flagged_projects": []}
    assert all(r["change_log"] == [] for r in db.tables["rfp_portal_invitations"])
    assert not any(_pending(db).values())


def test_delete_clears_unlinked_rows_rematches_the_parked_one_and_flags_linked_projects():
    db = _prop_db()
    out = ga.delete(db, "al-bc-co-1")
    assert out == {"updated": 5, "rematched": 1, "flagged_projects": ["p-1", "p-2"]}
    inv = _invs(db)
    for key in ("i-open", "i-parked", "i-review"):
        r = inv[key]
        assert (r["gc_id"], r["gc_kind"], r["gc_confirmed_at"], r["gc_confirmed_by"]) == (None, None, None, None), key
    # Only the row parked for its GC goes back to match, reset for the sweep.
    assert (inv["i-parked"]["status"], inv["i-parked"]["flag_reason"]) == ("match", None)
    assert (inv["i-parked"]["attempts"], inv["i-parked"]["last_error"], inv["i-parked"]["decided_at_step"]) == (0, None, None)
    assert inv["i-open"]["status"] == "done" and inv["i-review"]["status"] == "review_match"
    for key in ("i-created", "i-exists"):
        assert (inv[key]["gc_id"], inv[key]["gc_kind"]) == ("gc-mon", "alias")
        (entry,) = inv[key]["change_log"]
        assert (entry["field"], entry["old"], entry["new"]) == ("gc_alias", "gc-mon", None)
    assert _pending(db) == {"p-1": True, "p-2": True, "p-9": False}
    for key in ("i-prov", "i-other", "i-portal"):
        assert inv[key]["gc_id"] == "gc-mon", key


def test_propagation_caps_the_change_log():
    db = _prop_db()
    long_log = [{"at": "x", "field": "close_at", "old": i, "new": i + 1, "run_id": None} for i in range(50)]
    _invs(db)["i-created"]["change_log"] = long_log
    ga.delete(db, "al-bc-co-1")
    log = _invs(db)["i-created"]["change_log"]
    assert len(log) == 50 and log[-1]["field"] == "gc_alias" and log[0]["old"] == 1


def test_propagation_fences_on_status_and_gc_kind(monkeypatch):
    """A row a person confirmed (or the sweep moved) between the read and
    the write is left alone."""
    db = _prop_db()
    real = ga._linked_project

    def racing(row):
        if row["id"] == "i-open":
            _invs(db)["i-open"]["status"] = "match"          # the sweep moved it meanwhile
        return real(row)

    monkeypatch.setattr(ga, "_linked_project", racing)
    ga.delete(db, "al-bc-co-1")
    assert _invs(db)["i-open"]["gc_id"] == "gc-mon"
