"""The bidding-site link captured at intake (migration 0079).

Every project must answer the question one way or the other: a URL, or the
"this project has no link" flag. The two are mutually exclusive, and a later
PATCH may change the answer but never un-answer it. The scheme allow-list keeps
a `javascript:` payload out of the href the project page renders.
"""

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.models.schemas import ProjectCreate
from app.routers.projects import _apply_bidding_url_rules

BASE = {
    "name": "Acme Tower",
    "number": "G3-2026-001",
    "internal_bid_at": "2026-08-01T12:00:00Z",
    "invitation_at": "2026-07-01T12:00:00Z",
    "due_from_estimator_at": "2026-07-20T12:00:00Z",
    "due_from_vendors_at": "2026-07-25T12:00:00Z",
    "project_type": "other",
    "owner_type": "other",
    "labor_needed": "other",
    "bid_method": "other",
    "competitor_known": "other",
    "gc_known": "other",
    "subs_needed": "other",
    "est_value_band": "other",
    "scope_fit": "other",
}


def make(**overrides) -> ProjectCreate:
    return ProjectCreate(**{**BASE, **overrides})


# ── Intake must answer the question ─────────────────────────────────────────


def test_url_alone_is_accepted():
    p = make(bidding_url="https://app.buildingconnected.com/projects/abc")
    assert p.bidding_url == "https://app.buildingconnected.com/projects/abc"
    assert p.no_bidding_url is False


def test_no_link_flag_alone_is_accepted():
    p = make(no_bidding_url=True)
    assert p.bidding_url is None
    assert p.no_bidding_url is True


def test_neither_is_rejected():
    with pytest.raises(ValidationError, match="bidding_url is required"):
        make()


def test_blank_url_counts_as_unanswered():
    # A whitespace-only box is "not filled in", not a link.
    with pytest.raises(ValidationError, match="bidding_url is required"):
        make(bidding_url="   ")


def test_both_is_rejected():
    with pytest.raises(ValidationError, match="not both"):
        make(bidding_url="https://example.com/bid", no_bidding_url=True)


# ── The URL itself ──────────────────────────────────────────────────────────


def test_url_is_trimmed():
    assert make(bidding_url="  https://example.com/bid  ").bidding_url == "https://example.com/bid"


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",  # rendered as an href — the reason for the allow-list
        "data:text/html,<script>alert(1)</script>",
        "mailto:bids@example.com",
        "ftp://example.com/bid",
        "https://",  # scheme with nothing after it
        "//",  # protocol-relative with no host
    ],
)
def test_non_http_urls_are_rejected(url):
    with pytest.raises(ValidationError, match="http:// or https://"):
        make(bidding_url=url)


# ── Scheme-less pastes ──────────────────────────────────────────────────────
# Nobody types "https://" — they copy the host out of the invitation email. The
# scheme is filled in rather than made the user's problem.


@pytest.mark.parametrize(
    ("typed", "stored"),
    [
        (
            "app.buildingconnected.com/projects/abc",
            "https://app.buildingconnected.com/projects/abc",
        ),
        ("www.isqft.com/bid?id=7", "https://www.isqft.com/bid?id=7"),
        ("example.com", "https://example.com"),
        # Protocol-relative, as copied out of some page sources.
        ("//example.com/bid", "https://example.com/bid"),
        # A host with a port only looks like it carries a scheme.
        ("example.com:8080/bid", "https://example.com:8080/bid"),
        ("localhost:3000/bid", "https://localhost:3000/bid"),
        # Fumbled slashes after a real scheme.
        ("https:/example.com/bid", "https://example.com/bid"),
        ("https:example.com/bid", "https://example.com/bid"),
        # Case is the scheme's, not the host's — leave the rest alone.
        ("HTTPS://Example.com/Bid", "https://Example.com/Bid"),
        ("  app.buildingconnected.com/projects/abc  ", "https://app.buildingconnected.com/projects/abc"),
    ],
)
def test_missing_scheme_is_filled_in(typed, stored):
    assert make(bidding_url=typed).bidding_url == stored


def test_http_is_allowed():
    # Some GC portals are still plain http; the allow-list is about the scheme
    # being a web link, not about transport security.
    assert make(bidding_url="http://example.com/bid").bidding_url == "http://example.com/bid"


def test_overlong_url_is_rejected():
    with pytest.raises(ValidationError, match="2000 characters"):
        make(bidding_url="https://example.com/" + "a" * 2000)


# ── PATCH keeps the pair consistent ─────────────────────────────────────────


def test_patching_a_url_clears_the_no_link_flag():
    patch = {"bidding_url": "https://example.com/bid"}
    _apply_bidding_url_rules(patch)
    assert patch == {"bidding_url": "https://example.com/bid", "no_bidding_url": False}


def test_patching_the_no_link_flag_clears_the_url():
    patch = {"no_bidding_url": True}
    _apply_bidding_url_rules(patch)
    assert patch == {"no_bidding_url": True, "bidding_url": None}


def test_clearing_the_url_without_the_flag_is_rejected():
    with pytest.raises(HTTPException) as exc:
        _apply_bidding_url_rules({"bidding_url": None})
    assert exc.value.status_code == 400


def test_turning_off_the_flag_without_a_url_is_rejected():
    with pytest.raises(HTTPException) as exc:
        _apply_bidding_url_rules({"no_bidding_url": False})
    assert exc.value.status_code == 400


def test_unrelated_patches_are_untouched():
    patch = {"address": "11011 W Charleston Blvd"}
    _apply_bidding_url_rules(patch)
    assert patch == {"address": "11011 W Charleston Blvd"}
