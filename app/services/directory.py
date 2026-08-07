"""Company-name uniqueness for the GC and vendor directories.

One company must have exactly one directory row. Two rows for "ABC Electric"
and "abc electric" quietly fork it: the RFQ recipient list shows half the
contacts under each, returned quotes bind to whichever twin the sender picked,
and the bid-invitations report counts the same GC twice. Nothing downstream can
tell the twins apart afterwards, so the only cheap moment to stop it is the
insert.

Both directories are written from several places (the Contacts page, the New
Bid modal, a project's GC panel, the "new company" option in the RFQ step), so
the guard lives behind the two POST routes all of those funnel through rather
than in each form. A new form gets it for free.

Names are compared with case and surrounding/repeated whitespace ignored, and
are otherwise exact: companies whose names genuinely differ ("G3 Electric" vs
"G3 Electrical") are both still allowed. PEOPLE are deliberately not deduped
here, only companies. Two different people really can share a name, and
gc_contacts/vendor_contacts exist precisely to hold several of them per
company.
"""

from typing import Any


def normalize_company_name(name: str | None) -> str:
    """The form two company names are compared in.

    Case-folded, trimmed, and internal whitespace runs collapsed: a user cannot
    see the difference between "ABC  Electric" and "ABC Electric", so treating
    them as distinct companies would make the guard trivially bypassable by
    accident.
    """
    return " ".join((name or "").split()).casefold()


def clean_company_name(name: str | None) -> str:
    """The form a company name is STORED in: same normalization, original case.

    Applied on insert so what is stored is what the guard compares, and so a
    stray trailing space cannot slip a near-twin past a later check.
    """
    return " ".join((name or "").split())


def find_duplicate_company(sb: Any, table: str, name: str) -> dict | None:
    """The existing row in `table` whose name matches `name`, or None.

    Matched in Python rather than with a PostgREST ``ilike``: ``%`` and ``_``
    are wildcards in an ilike pattern and may appear verbatim in a company
    name, so a pattern match would both over- and under-match. Both directories
    hold hundreds of rows at most and this runs only when someone adds a
    company, so reading the name column is cheaper than getting that wrong.
    """
    target = normalize_company_name(name)
    if not target:
        return None
    rows = (sb.table(table).select("id, name").execute()).data or []
    for row in rows:
        if normalize_company_name(row.get("name")) == target:
            return row
    return None


def duplicate_company_message(kind: str, existing_name: str) -> str:
    """The 409 body a user reads. Names the row that already exists.

    Quoting the stored spelling matters: the user typed "abc electric" and the
    directory says "ABC Electric", so without it the list looks like it has no
    such company and the error reads as a bug.
    """
    return (
        f'"{existing_name}" is already in the system as a {kind}. '
        "Select the existing company instead of adding it again."
    )
