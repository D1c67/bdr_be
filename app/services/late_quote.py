"""Late AI-extracted quote helpers (Phase 7 WI-10, plan 4.5).

A vendor quote the RFQ poller extracts after the bid was submitted is belled
as `late_quote.received`, like the manual paths in routers/rfqs.py. This module
holds the one pure piece: the bell text. It is a copy of the text
`rfqs._notify_late_quote` builds, pinned equal by a test, so routers/rfqs.py
stays untouched; switching that router to this helper is a later import swap.
"""


def late_quote_message(project: dict | None, vendor: str | None, category: str | None) -> str:
    """The bell text for a quote that arrived after the bid was submitted.

    `project` is a dict with optional `number` and `name`; `vendor` and
    `category` are plain names. Missing values fall back as in the manual
    paths ("a project", "a vendor", "a category")."""
    p = project or {}
    label = " ".join(x for x in (p.get("number"), p.get("name")) if x) or "a project"
    return (
        f"New quote received for {label} from {vendor or 'a vendor'} "
        f"({category or 'a category'}), after the bid was submitted."
    )
