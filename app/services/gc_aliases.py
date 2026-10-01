"""External GC aliases: which BDR general contractor an outside system's
company is (docs/RFP_BUILDINGCONNECTED.md section 3.4, scratchpad
BUILD_CONTRACT.md section 3.3).

BuildingConnected names the inviting GC by its own company id and name
(`client.company.id`, `client.company.name`). Those never equal a
general_contractors row, so the first time a company is seen a person
confirms which GC it is and the answer is kept in `gc_external_aliases`
keyed on (source, external_id). Every later invitation from that company
resolves through the alias with no question asked, even after the company
renames itself on the platform (the stored external_name just follows it).

`resolve` runs in this order and stops at the first hit:

1. a confirmed alias for (source, external_id). The aliases are read once
   per sweep and cached on the reference bundle under `gc_aliases`;
2. the lead's email through `rfp_match.resolve_gc` in its `gc_domain`
   shape: an exact contact address (case folded; a plus-tagged address also
   tries its untagged form) or a non-public domain exactly one GC owns.
   Only the contact and domain kinds are accepted from it; its name path
   (threshold plus runner-up gap) is not used here;
3. provisional: the most similar GC name (provisional_score, stricter
   than rfp_match._similarity about generic words and a single shared
   word) when it scores at least PROVISIONAL_MIN_SCORE, with the top three
   candidates in resolve_gc's `{gc_id, name, score}` shape;
4. none, when there are no GCs to compare with, no external name, or the
   best name scores below PROVISIONAL_MIN_SCORE. The candidates are still
   listed then, so the GC card can offer the closest names.

Alias, contact and domain count as confirmed for matching
(CONFIRMED_KINDS); provisional and none park the invitation for a person.
A provisional GC is linked to a created project but never gets the lead
filed as its contact (rfp_create); the GC card's answer does that.

Repointing or deleting an alias follows through to the invitations
resolved through it (`_propagate`): rows without a project take the new
GC (or lose it), rows on a project leave the project alone and reopen its
GC card.

Everything here uses the sync Supabase client: callers are plain `def`
routes, the queue worker or `run_in_threadpool`, never an `async def`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parseaddr
from typing import Any

from app.services import directory, rfp_match

logger = logging.getLogger(__name__)

SOURCE_BC = "buildingconnected"

KIND_ALIAS, KIND_CONTACT, KIND_DOMAIN, KIND_PROVISIONAL, KIND_NONE = (
    "alias", "contact", "domain", "provisional", "none",
)
CONFIRMED_KINDS = (KIND_ALIAS, KIND_CONTACT, KIND_DOMAIN)

TABLE = "gc_external_aliases"
BUNDLE_KEY = "gc_aliases"
# Per-bundle cache of every GC name's (words, distinctive words), keyed by
# the raw name, so a sweep normalizes each GC name once, not once per row.
WORDS_BUNDLE_KEY = "gc_alias_name_words"
CANDIDATE_COUNT = 3
INVITATIONS_TABLE = "rfp_portal_invitations"

# The provisional guess needs at least this provisional_score. Below it the
# best name is not evidence of anything (the dev run of 2026-09-28 linked an
# unrelated company to the alphabetically first GC at 0.0), so resolve()
# answers `none` and the row parks for a person. 0.5 is where the GC card
# already stops presenting the guess as the primary answer ("No close match
# in BDR"). A module constant, not a Settings field: tune it here.
PROVISIONAL_MIN_SCORE = 0.5

_ALIAS_SELECT = "id, source, external_id, external_name, gc_id, confirmed_by, confirmed_at"
_PROPAGATE_SELECT = "id, status, flag_reason, gc_id, gc_kind, created_project_id, match_project_id, change_log"
# rfp_portal_ingest.STATUS_EXISTS / STATUS_CREATED: an invitation at either
# sits on a project.
_LINKED_STATUSES = ("exists", "created")
# rfp_bc_portal.FLAG_GC_UNRESOLVED: parked because the GC was unresolved.
_FLAG_GC_UNRESOLVED = rfp_match.REASON_GC_UNRESOLVED


class GcAliasError(LookupError):
    """A named row is missing. `code` is the machine reason a router maps."""

    code = "gc_alias_error"


class AliasNotFound(GcAliasError):
    code = "gc_alias_not_found"


class GcNotFound(GcAliasError):
    code = "gc_not_found"


@dataclass
class GcResolve:
    kind: str
    gc_id: str | None
    gc_name: str | None
    contact_id: str | None
    candidates: list[dict] = field(default_factory=list)  # [{gc_id, name, score}] top 3


# ── small helpers ────────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bundle_get(bundle, name: str):
    if bundle is None:
        return None
    if isinstance(bundle, dict):
        return bundle.get(name)
    return getattr(bundle, name, None)


def _bundle_set(bundle, name: str, value) -> None:
    if bundle is None:
        return
    if isinstance(bundle, dict):
        bundle[name] = value
        return
    try:
        setattr(bundle, name, value)
    except (AttributeError, TypeError):
        pass


def normalize_email(address: str | None) -> str:
    """Lowercased bare address ('' when there is none). Accepts a display
    form such as 'Jane Doe <Jane@GC.com>'."""
    text = (address or "").strip()
    if not text:
        return ""
    if "<" in text:
        text = parseaddr(text)[1] or text
    text = text.strip().lower()
    if text.startswith("mailto:"):
        text = text[len("mailto:"):]
    return text if "@" in text else ""


def untagged_email(address: str) -> str | None:
    """`jane+bids@gc.com` -> `jane@gc.com`; None when there is no plus tag."""
    if "@" not in address:
        return None
    local, domain = address.rsplit("@", 1)
    if "+" not in local:
        return None
    base = local.split("+", 1)[0]
    if not base:
        return None
    return f"{base}@{domain}"


def _gcs(bundle) -> list[dict]:
    return [g for g in (_bundle_get(bundle, "gcs") or []) if g.get("id")]


# ── provisional name score ───────────────────────────────────────────────────

# Words that say what kind of firm a company is, not which firm. The name
# drop list of rfp_match.normalize_gc_name is included; the rest are words
# it keeps ("building", "services", "companies") that must not carry a
# match on their own.
GENERIC_WORDS = frozenset(
    {
        "construction", "builders", "building", "company", "companies",
        "contractors", "contracting", "group", "services", "inc", "llc", "corp",
        "corporation", "enterprises", "international", "general", "associates",
        "the", "and", "of",
    }
) | rfp_match._GC_DROP_TOKENS

# One shared distinctive word, with the names opening on different words,
# is weak evidence: the score is scaled by this.
OFF_LEAD_FACTOR = 0.3
# A single-word name counts as contained in a longer one only from this
# length up ("Catamount" yes; "One", "Core", "Tre" no, Dice decides those).
_MIN_SINGLE_WORD_LEN = 6


def _name_words(name) -> tuple[list[str], list[str]]:
    """(every word, the distinctive words) of a company name: NFKC,
    lowercased, punctuation to spaces, a run of single letters joined into
    one word ("J.E. Dunn" and "JE Dunn" both open on "je"); the distinctive
    words are what is left after GENERIC_WORDS."""
    base = rfp_match._prepare(directory.normalize_company_name(name))
    words: list[str] = []
    joining = False
    for word in rfp_match._NON_ALNUM.sub(" ", base).split():
        initial = len(word) == 1 and word.isalpha()
        if initial and joining:
            words[-1] += word
        else:
            words.append(word)
        joining = initial
    return words, [w for w in words if w not in GENERIC_WORDS]


def _lead(words: list[str]) -> str | None:
    """The word a name opens with, a leading 'the' skipped."""
    for word in words:
        if word != "the":
            return word
    return None


def _word_containment(a_core: list[str], b_core: list[str]) -> float:
    """Shared distinctive words over the shorter name's count, counted only
    when at least two are shared, or when the shorter name is one word of
    _MIN_SINGLE_WORD_LEN or more found in the other (then 1.0)."""
    sa, sb = set(a_core), set(b_core)
    shared = sa & sb
    smaller = sa if len(sa) <= len(sb) else sb
    if len(shared) >= 2:
        return len(shared) / len(smaller)
    if len(smaller) == 1 and shared and len(next(iter(shared))) >= _MIN_SINGLE_WORD_LEN:
        return 1.0
    return 0.0


def provisional_score(a_name, b_name) -> float:
    """How alike two company names are for the provisional GC guess, 0 to 1.

    max(trigram Dice, word containment) over the distinctive words (the
    name without GENERIC_WORDS), where containment needs two shared words
    or a long single-word name (_word_containment). When the names share
    exactly one distinctive word and open with different words ("United
    Construction Company" vs "Builders United", "Construction One" vs
    "Eagle One Construction") the score is scaled by OFF_LEAD_FACTOR. Two
    names made only of generic words are compared by the Dice of the whole
    names; one such name against a distinctive one scores 0.

    Takes the raw names, unlike rfp_match._similarity (which the email
    matcher keeps with its own threshold and gap): normalize_gc_name has
    already dropped the words the opening-word check reads."""
    return _score_words(_name_words(a_name), _name_words(b_name))


def _score_words(a: tuple[list[str], list[str]], b: tuple[list[str], list[str]]) -> float:
    """provisional_score over two `_name_words` results."""
    a_words, a_core = a
    b_words, b_core = b
    if not a_words or not b_words:
        return 0.0
    if not a_core and not b_core:
        return rfp_match._dice(" ".join(a_words), " ".join(b_words))
    if not a_core or not b_core:
        return 0.0
    score = max(
        rfp_match._dice(" ".join(a_core), " ".join(b_core)),
        _word_containment(a_core, b_core),
    )
    if len(set(a_core) & set(b_core)) == 1 and _lead(a_words) != _lead(b_words):
        score *= OFF_LEAD_FACTOR
    return score


def _gc_words(bundle, name) -> tuple[list[str], list[str]]:
    """`_name_words(name)` for a GC name, cached on the bundle under
    WORDS_BUNDLE_KEY (keyed by the raw name, so a renamed GC is simply a
    new key). Without a bundle it is computed each time."""
    cache = _bundle_get(bundle, WORDS_BUNDLE_KEY)
    if cache is None:
        cache = {}
        _bundle_set(bundle, WORDS_BUNDLE_KEY, cache)
    key = "" if name is None else str(name)
    hit = cache.get(key)
    if hit is None:
        hit = _name_words(name)
        cache[key] = hit
    return hit


def _candidates(external_name: str | None, gcs: list[dict], bundle=None) -> list[dict]:
    """The top three GCs by provisional_score, ties broken by name, in
    resolve_gc's `{gc_id, name, score}` shape. Empty without a name. A
    name made only of generic words ("The Builders Group") still ranks:
    provisional_score compares such names by their whole words."""
    ext = _name_words(external_name)
    if not ext[0]:
        return []
    scored: list[tuple[float, str, dict]] = []
    for gc in gcs:
        score = _score_words(ext, _gc_words(bundle, gc.get("name")))
        scored.append((score, str(gc.get("name") or ""), gc))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [
        {"gc_id": gc["id"], "name": gc.get("name"), "score": round(score, 3)}
        for score, _, gc in scored[:CANDIDATE_COUNT]
    ]


def _gc_row(sb, gc_id: str) -> dict | None:
    rows = (
        sb.table("general_contractors").select("id, name").eq("id", gc_id).limit(1).execute()
    ).data or []
    return rows[0] if rows else None


def _gc_name(sb, gc_id: str, gcs: list[dict]) -> str | None:
    for gc in gcs:
        if str(gc.get("id")) == str(gc_id):
            return gc.get("name")
    try:
        row = _gc_row(sb, gc_id)
    except Exception:  # noqa: BLE001
        logger.exception("gc aliases: GC name lookup failed for %s", gc_id)
        return None
    return (row or {}).get("name")


def _bundle_contact(bundle, gc_id: str, email: str) -> str | None:
    """The bundle contact under `gc_id` whose address is `email` (or its
    untagged form)."""
    if not email:
        return None
    wanted = {email}
    plain = untagged_email(email)
    if plain:
        wanted.add(plain)
    for contact in _bundle_get(bundle, "contacts") or []:
        if str(contact.get("gc_id")) != str(gc_id):
            continue
        if (contact.get("email") or "").strip().lower() in wanted:
            return contact.get("id")
    return None


# ── resolve ──────────────────────────────────────────────────────────────────


def load_aliases(sb, source: str) -> dict[str, dict]:
    """Every alias for `source`, keyed by external_id."""
    rows = (sb.table(TABLE).select(_ALIAS_SELECT).eq("source", source).execute()).data or []
    return {str(r["external_id"]): r for r in rows if r.get("external_id") is not None}


def _aliases_for(sb, source: str, bundle) -> dict[str, dict]:
    cached = _bundle_get(bundle, BUNDLE_KEY)
    if cached is not None:
        return cached
    aliases = load_aliases(sb, source)
    _bundle_set(bundle, BUNDLE_KEY, aliases)
    return aliases


def _by_email(email: str, bundle, settings):
    """rfp_match.resolve_gc in its gc_domain shape; only contact and domain
    answers count. A plus-tagged address that did not hit a contact retries
    its untagged form for a contact hit. No name is passed: its name path
    is never accepted here, so it would only be a wasted scoring loop."""

    def ask(address: str):
        res = rfp_match.resolve_gc(
            {
                "authorization_kind": "gc_domain",
                "from_address": address,
                "extracted_gc_name": None,
            },
            bundle,
            settings,
        )
        if res.kind in (rfp_match.GC_KIND_CONTACT, rfp_match.GC_KIND_DOMAIN) and res.gc_id:
            return res
        return None

    first = ask(email)
    if first is not None and first.kind == rfp_match.GC_KIND_CONTACT:
        return first
    plain = untagged_email(email)
    if plain:
        second = ask(plain)
        if second is not None and second.kind == rfp_match.GC_KIND_CONTACT:
            return second
    return first


def resolve(
    sb,
    *,
    source: str,
    external_id: str | None,
    external_name: str | None,
    lead_email: str | None,
    bundle,
    settings,
) -> GcResolve:
    """Which GC this external company is (module docstring for the order)."""
    gcs = _gcs(bundle)
    candidates = _candidates(external_name, gcs, bundle)
    email = normalize_email(lead_email)

    # 1. confirmed alias
    ext = str(external_id).strip() if external_id is not None else ""
    if ext:
        aliases = _aliases_for(sb, source, bundle)
        row = aliases.get(ext)
        if row and row.get("gc_id"):
            new_name = (external_name or "").strip()
            if new_name and new_name != (row.get("external_name") or ""):
                try:
                    rename_external(sb, source=source, external_id=ext, external_name=new_name)
                    row["external_name"] = new_name
                except Exception:  # noqa: BLE001
                    logger.exception("gc aliases: rename failed for %s/%s", source, ext)
            gc_id = str(row["gc_id"])
            return GcResolve(
                kind=KIND_ALIAS,
                gc_id=gc_id,
                gc_name=_gc_name(sb, gc_id, gcs),
                contact_id=_bundle_contact(bundle, gc_id, email),
                candidates=candidates,
            )

    # 2. lead contact address, then a domain one GC owns
    if email:
        hit = _by_email(email, bundle, settings)
        if hit is not None:
            gc_id = str(hit.gc_id)
            kind = KIND_CONTACT if hit.kind == rfp_match.GC_KIND_CONTACT else KIND_DOMAIN
            return GcResolve(
                kind=kind,
                gc_id=gc_id,
                gc_name=_gc_name(sb, gc_id, gcs),
                contact_id=hit.contact_id if kind == KIND_CONTACT else None,
                candidates=candidates,
            )

    # 3. provisional: the most similar name, when it clears the floor
    if candidates and float(candidates[0].get("score") or 0.0) >= PROVISIONAL_MIN_SCORE:
        best = candidates[0]
        return GcResolve(
            kind=KIND_PROVISIONAL,
            gc_id=str(best["gc_id"]),
            gc_name=best.get("name"),
            contact_id=None,
            candidates=candidates,
        )

    # 4. nothing close enough (the candidates stay for the card's picker)
    return GcResolve(kind=KIND_NONE, gc_id=None, gc_name=None, contact_id=None, candidates=candidates)


# ── writes ───────────────────────────────────────────────────────────────────


def _require_gc(sb, gc_id: str) -> dict:
    row = _gc_row(sb, str(gc_id)) if gc_id else None
    if not row:
        raise GcNotFound(f"GC {gc_id} not found")
    return row


def confirm(sb, *, source, external_id, external_name, gc_id, actor_id) -> dict:
    """Record (or re-point) the alias for (source, external_id). Idempotent:
    the unique (source, external_id) row is upserted and its external_name
    follows the latest spelling."""
    ext = str(external_id or "").strip()
    if not ext:
        raise ValueError("external_id is required")
    _require_gc(sb, gc_id)
    payload = {
        "source": source,
        "external_id": ext,
        "external_name": (external_name or "").strip() or ext,
        "gc_id": str(gc_id),
        "confirmed_by": actor_id,
        "confirmed_at": _now_iso(),
    }
    rows = (sb.table(TABLE).upsert(payload, on_conflict="source,external_id").execute()).data or []
    if rows:
        return rows[0]
    rows = (
        sb.table(TABLE).select(_ALIAS_SELECT)
        .eq("source", source).eq("external_id", ext).limit(1).execute()
    ).data or []
    return rows[0] if rows else payload


# The tables that point at a general_contractors row. A GC this module
# inserted is removed again only when none of them refers to it (a cascade
# would take someone else's rows with it; a restrict would fail anyway).
_GC_REFERENCES = (
    ("gc_contacts", "gc_id"),
    ("project_gcs", "gc_id"),
    (TABLE, "gc_id"),
    (INVITATIONS_TABLE, "gc_id"),
    ("rfp_project_matches", "gc_id"),
    ("proposal_sends", "gc_id"),
    ("proposal_send_events", "gc_id"),
    ("bid_gc_outcomes", "gc_id"),
)


def _gc_referenced(sb, gc_id: str) -> bool:
    for table, column in _GC_REFERENCES:
        if (sb.table(table).select("id").eq(column, gc_id).limit(1).execute()).data:
            return True
    return False


def _delete_gc_if_unlinked(sb, gc_id: str) -> bool:
    """Take back a GC row this module inserted, unless anything refers to
    it by now. Best effort: an error keeps the row. True when deleted."""
    try:
        if _gc_referenced(sb, gc_id):
            logger.warning("gc aliases: kept GC %s it inserted: something refers to it", gc_id)
            return False
        deleted = (sb.table("general_contractors").delete().eq("id", gc_id).execute()).data or []
    except Exception:  # noqa: BLE001
        logger.exception("gc aliases: could not take back GC %s", gc_id)
        return False
    return bool(deleted)


def _oldest_twin(sb, clean: str) -> dict | None:
    """The oldest general_contractors row whose name is `clean` under
    directory.normalize_company_name (created_at, then id), or None."""
    target = directory.normalize_company_name(clean)
    rows = (sb.table("general_contractors").select("id, name, created_at").execute()).data or []
    twins = [r for r in rows if r.get("id") and directory.normalize_company_name(r.get("name")) == target]
    if not twins:
        return None
    twins.sort(key=lambda r: (r.get("created_at") is None, str(r.get("created_at") or ""), str(r["id"])))
    return twins[0]


def create_gc_and_confirm(
    sb,
    *,
    source,
    external_id,
    external_name,
    name,
    contact_name,
    contact_email,
    contact_phone,
    actor_id,
) -> dict:
    """Add the GC a person named (reusing the directory twin when one exists,
    so no second row for the same company is ever made), add the contact
    when an email is given (reusing one with the same address under that
    GC, rfp_create.ensure_lead_contact), and confirm the alias to it.

    There is no unique index on the GC name, so the twin check runs twice:
    before the insert, and again right after it, when a twin inserted
    concurrently by someone else would show. The older row wins; when that
    is the other one, the row just inserted is deleted and the older one is
    reused. A failure after the insert (the contact, the alias) takes back
    the contact and the GC this call inserted (the GC only while nothing
    refers to it) and re-raises.

    Returns {alias, gc_id, gc_name, contact_id, reused}."""
    from app.services import rfp_create  # local: rfp_create is the heavier module

    clean = directory.clean_company_name(name) or directory.clean_company_name(external_name)
    if not clean:
        raise ValueError("A GC name is required")
    inserted_gc_id: str | None = None
    existing = directory.find_duplicate_company(sb, "general_contractors", clean)
    if existing:
        gc_id, gc_name, reused = str(existing["id"]), existing.get("name") or clean, True
    else:
        inserted = (sb.table("general_contractors").insert({"name": clean}).execute()).data or []
        new_id = str(inserted[0]["id"])
        try:
            winner = _oldest_twin(sb, clean)
        except Exception:
            _delete_gc_if_unlinked(sb, new_id)
            raise
        if winner is not None and str(winner["id"]) != new_id:
            logger.info("gc aliases: GC %r was added concurrently; reusing %s", clean, winner["id"])
            _delete_gc_if_unlinked(sb, new_id)
            gc_id, gc_name, reused = str(winner["id"]), winner.get("name") or clean, True
        else:
            gc_id, gc_name, reused = new_id, clean, False
            inserted_gc_id = new_id

    contact_id = None
    inserted_contact_id: str | None = None
    try:
        email = normalize_email(contact_email)
        if email:
            contact_id, made = rfp_create.ensure_lead_contact(
                sb, gc_id, {"first_name": contact_name, "email": email, "phone": contact_phone},
            )
            if made:
                inserted_contact_id = contact_id
        alias = confirm(
            sb, source=source, external_id=external_id, external_name=external_name,
            gc_id=gc_id, actor_id=actor_id,
        )
    except Exception:
        if inserted_contact_id:
            try:
                sb.table("gc_contacts").delete().eq("id", inserted_contact_id).eq("gc_id", gc_id).execute()
            except Exception:  # noqa: BLE001
                logger.exception("gc aliases: could not take back contact %s", inserted_contact_id)
        if inserted_gc_id:
            _delete_gc_if_unlinked(sb, inserted_gc_id)
        raise
    return {"alias": alias, "gc_id": gc_id, "gc_name": gc_name, "contact_id": contact_id, "reused": reused}


def list_aliases(sb, source) -> list[dict]:
    """Aliases for `source` in the API shape: {id, source, external_id,
    external_name, gc: {id, name}, confirmed_by: {id, name} | None,
    confirmed_at}, ordered by external name."""
    rows = (
        sb.table(TABLE).select(_ALIAS_SELECT).eq("source", source)
        .order("external_name").order("id").execute()
    ).data or []
    gc_ids = sorted({str(r["gc_id"]) for r in rows if r.get("gc_id")})
    actor_ids = sorted({str(r["confirmed_by"]) for r in rows if r.get("confirmed_by")})
    gc_names: dict[str, Any] = {}
    if gc_ids:
        for g in (sb.table("general_contractors").select("id, name").in_("id", gc_ids).execute()).data or []:
            gc_names[str(g["id"])] = g.get("name")
    actor_names: dict[str, Any] = {}
    if actor_ids:
        for p in (sb.table("profiles").select("id, full_name").in_("id", actor_ids).execute()).data or []:
            actor_names[str(p["id"])] = p.get("full_name")
    out = []
    for r in rows:
        gc_id = str(r["gc_id"]) if r.get("gc_id") else None
        actor = str(r["confirmed_by"]) if r.get("confirmed_by") else None
        out.append({
            "id": r["id"],
            "source": r.get("source"),
            "external_id": r.get("external_id"),
            "external_name": r.get("external_name"),
            "gc": {"id": gc_id, "name": gc_names.get(gc_id) if gc_id else None},
            "confirmed_by": {"id": actor, "name": actor_names.get(actor)} if actor else None,
            "confirmed_at": r.get("confirmed_at"),
        })
    return out


def _alias_row(sb, alias_id) -> dict:
    rows = (sb.table(TABLE).select(_ALIAS_SELECT).eq("id", alias_id).limit(1).execute()).data or []
    if not rows:
        raise AliasNotFound(f"Alias {alias_id} not found")
    return rows[0]


def repoint(sb, alias_id, gc_id, actor_id) -> dict:
    """Point an alias at a different GC (IT Admin correction), then follow
    through to the invitations resolved through it (`_propagate`). The
    alias row comes back with `propagated` (the _propagate summary)."""
    _require_gc(sb, gc_id)
    before = _alias_row(sb, alias_id)
    rows = (
        sb.table(TABLE)
        .update({"gc_id": str(gc_id), "confirmed_by": actor_id, "confirmed_at": _now_iso()})
        .eq("id", alias_id)
        .execute()
    ).data or []
    if not rows:
        raise AliasNotFound(f"Alias {alias_id} not found")
    row = dict(rows[0])
    row["propagated"] = _propagate(sb, before, str(gc_id))
    return row


def delete(sb, alias_id) -> dict:
    """Forget an alias: the company is asked about again next time, and the
    invitations resolved through it follow (`_propagate`). Returns the
    _propagate summary."""
    rows = (sb.table(TABLE).delete().eq("id", alias_id).execute()).data or []
    if not rows:
        raise AliasNotFound(f"Alias {alias_id} not found")
    return _propagate(sb, rows[0], None)


def _linked_project(row: dict) -> tuple[bool, str | None]:
    """(linked, project id) for an invitation row, the rfp_bc_portal rule:
    a row that created a project, or one sitting on a project at `exists`
    / `created`, is linked."""
    created = row.get("created_project_id")
    if created:
        return True, str(created)
    if row.get("status") in _LINKED_STATUSES:
        project = row.get("match_project_id")
        return True, str(project) if project else None
    return False, None


def _propagate(sb, alias: dict, new_gc_id: str | None) -> dict:
    """Carry an alias repoint (`new_gc_id`) or delete (None) onto the
    invitations of the same (source, external_id) whose GC came through an
    alias (gc_kind `alias`):

    - no project: a repoint writes the new gc_id; a delete clears gc_id,
      gc_kind and the confirmation, and a row parked at review_match for
      the unresolved GC goes back to `match` to be resolved again;
    - on a project: the project is never rewritten; its
      `gc_confirm_pending` is set so the GC card asks again, and the row's
      change_log records {field: "gc_alias", old, new}.

    Every write is fenced on the row's status and gc_kind, so a row a
    person or the sweep moved meanwhile is left alone. Best effort per
    row: the alias change itself already stands. Returns {updated,
    rematched, flagged_projects}."""
    from app.services import rfp_portal_ingest as pi  # local: avoids an import cycle

    out = {"updated": 0, "rematched": 0, "flagged_projects": []}
    source = str(alias.get("source") or "").strip()
    ext = str(alias.get("external_id") or "").strip()
    old_gc_id = str(alias["gc_id"]) if alias.get("gc_id") else None
    if not source or not ext or (new_gc_id is not None and new_gc_id == old_gc_id):
        return out
    try:
        rows = (
            sb.table(INVITATIONS_TABLE).select(_PROPAGATE_SELECT)
            .eq("portal", source).eq("gc_external_id", ext).eq("gc_kind", KIND_ALIAS)
            .execute()
        ).data or []
    except Exception:  # noqa: BLE001
        logger.exception("gc aliases: could not read the invitations of %s/%s", source, ext)
        return out
    now = _now_iso()
    flagged: list[str] = []
    for row in rows:
        try:
            linked, project_id = _linked_project(row)
            if linked:
                entry = {"at": now, "field": "gc_alias", "old": old_gc_id, "new": new_gc_id, "run_id": None}
                log = (list(row.get("change_log") or []) + [entry])[-pi._CHANGE_LOG_CAP:]
                sb.table(INVITATIONS_TABLE).update({"change_log": log}).eq("id", row["id"]).execute()
                out["updated"] += 1
                if project_id and project_id not in flagged:
                    sb.table("projects").update({"gc_confirm_pending": True}).eq("id", project_id).execute()
                    flagged.append(project_id)
                continue
            if new_gc_id is not None:
                update: dict = {"gc_id": new_gc_id}
            else:
                update = {"gc_id": None, "gc_kind": None, "gc_confirmed_at": None, "gc_confirmed_by": None}
                if row.get("status") == pi.STATUS_REVIEW_MATCH and row.get("flag_reason") == _FLAG_GC_UNRESOLVED:
                    update.update({"status": pi.STATUS_MATCH, "flag_reason": None, "decided_at_step": None, **pi._RESET})
            query = (
                sb.table(INVITATIONS_TABLE).update(update)
                .eq("id", row["id"]).eq("status", row.get("status")).eq("gc_kind", KIND_ALIAS)
                .is_("created_project_id", "null")
            )
            if (query.execute()).data:
                out["updated"] += 1
                if update.get("status") == pi.STATUS_MATCH:
                    out["rematched"] += 1
        except Exception:  # noqa: BLE001
            logger.exception("gc aliases: could not carry the alias change onto invitation %s", row.get("id"))
    out["flagged_projects"] = flagged
    return out


def rename_external(sb, *, source, external_id, external_name) -> None:
    """The platform renamed the company: follow the new spelling on the
    alias without asking anyone again. A blank name is ignored."""
    new_name = (external_name or "").strip()
    ext = str(external_id or "").strip()
    if not new_name or not ext:
        return
    (
        sb.table(TABLE)
        .update({"external_name": new_name})
        .eq("source", source)
        .eq("external_id", ext)
        .neq("external_name", new_name)
        .execute()
    )
