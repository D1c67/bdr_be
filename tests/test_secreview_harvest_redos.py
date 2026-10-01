"""Security review regression: harvested PipelineSuite scope text must not
drive a quadratic regex (ReDoS). A long run of word characters with no '@'
used to take seconds to minutes in _PS_EMAIL_RE.search."""

import time

from app.services import pipelinesuite_client as psc
from app.services import rfp_harvest as h


def _page(scope: str) -> psc.ProjectPage:
    return psc.ProjectPage(
        logged_in=True,
        gc_name="GC",
        title="Proj",
        invited_name=None,
        response_recorded=False,
        trades=[],
        info={"scope": scope},
        notices=[],
        contacts=[],
        files=[],
    )


def test_point_of_contact_pathological_scope_is_fast():
    scope = "a" * 60_000
    start = time.perf_counter()
    assert h._ps_point_of_contact(_page(scope)) is None
    assert time.perf_counter() - start < 1.0


def test_description_pathological_scope_is_fast():
    scope = "a" * 60_000
    start = time.perf_counter()
    out = h.pipelinesuite_description(scope)
    assert time.perf_counter() - start < 1.0
    assert out is not None and len(out) <= h._TEXT_MAX_CHARS


def test_point_of_contact_still_finds_rfi_contact_email():
    scope = "CLICK YES to bid\nPlease contact JOHN SMITH with any RFIs at John.Smith@Example-GC.com today."
    poc = h._ps_point_of_contact(_page(scope))
    assert poc == {"name": "John Smith", "email": "john.smith@example-gc.com", "phone": None}


def test_email_regex_is_linear_on_long_local_part():
    text = "a" * 60_000 + "@"
    start = time.perf_counter()
    assert h._PS_EMAIL_RE.search(text) is None
    assert time.perf_counter() - start < 1.0
