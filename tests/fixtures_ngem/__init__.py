"""Scrubbed NGEM portal pages for the client tests (docs/RFP_NGEM_PORTAL.md).

Captured 2026-09-15 from supplier.ionwave.net with the company's supplier
account over plain httpx (the browser captures are noted per file). What
changed, and nothing else:

- every `?e=<token>` value is a short dummy, distinct per link and stable
  across the pages (`list01` is the entry token in every form action,
  `event03` the UNLV row's view link on page 1 and the event page's own form
  token, `event41` the token the event page's tab strip carries, `file01`
  to `file08` the eight download links);
- the account name in the profile menu is `VENDORUSER`;
- the agency contact on the event page is `Pat Example`, `(702) 555-0100`,
  `contact@example.gov`.

The grids, the pager markup, the Telerik initializers and the `__VIEWSTATE`
blobs are byte-identical to the capture, so the parsers and the postback
builder run over the real shapes.

  login_page.html     GET /VendorLogin.aspx, anonymous (entry -> /Login.aspx -> here)
  list_page1.html     ResponseList.aspx after login: My Invitations page 1 of 4, 37 items
  list_page2.html     the same page after the pager postback to page 2 (ctl06)
  event_details.html  VResponseEvent.aspx for the UNLV 5584-GS Addendum 1 bid
  attachments.html    VResponseBidAttachments.aspx for that bid, 8 files, 1 page
  bad_request.html    /BadRequest.aspx, what a stale ?e= token answers
  error_page.html     /Error.aspx, reconstructed from the captured text
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

DIR = Path(__file__).resolve().parent

LOGIN_PAGE = "login_page.html"
LIST_PAGE_1 = "list_page1.html"
LIST_PAGE_2 = "list_page2.html"
EVENT_PAGE = "event_details.html"
ATTACHMENTS_PAGE = "attachments.html"
BAD_REQUEST_PAGE = "bad_request.html"
ERROR_PAGE = "error_page.html"

ALL_PAGES = (
    LOGIN_PAGE,
    LIST_PAGE_1,
    LIST_PAGE_2,
    EVENT_PAGE,
    ATTACHMENTS_PAGE,
    BAD_REQUEST_PAGE,
    ERROR_PAGE,
)

BASE = "https://supplier.ionwave.net"

# The dummy tokens the scrub left behind, by role.
ENTRY_TOKEN = "list01"                     # the form action of both list pages
EVENT_TOKEN = "event03"                    # the UNLV row's view link (page 1, row 3)
EVENT_TAB_TOKEN = "event41"                # the event page's tab strip links
ATTACHMENTS_TAB_TOKEN = "event42"          # the attachments page's tab strip links
FILE_TOKENS = tuple(f"file{n:02d}" for n in range(1, 9))

ENTRY_URL = f"{BASE}/VendorResponse/ResponseList.aspx?e={ENTRY_TOKEN}"
EVENT_URL = f"{BASE}/VendorResponse/Bid/VResponseEvent.aspx?e={EVENT_TOKEN}"
ATTACHMENTS_URL = f"{BASE}/VendorResponse/Bid/VResponseBidAttachments.aspx?e={EVENT_TAB_TOKEN}"
DOWNLOAD_URLS = tuple(f"{BASE}/Extract.aspx?e={token}" for token in FILE_TOKENS)

# The invited grid's pager targets as served on page 1 (page 1 = ctl05).
PAGER_PREFIX = "ctl00$mainContent$ucInvitedListGrid$rgResponse$ctl00$ctl03$ctl01$"
PAGE_TARGETS = {n: f"{PAGER_PREFIX}ctl{4 + n:02d}" for n in range(1, 5)}
ATTACHMENTS_PAGER_PREFIX = "ctl00$mainContent$rgBidAttachments$ctl00$ctl03$ctl01$"

# The checkbox text the login page's RadCheckBox initializer carries.
AGREE_TEXT = "I agree to the terms and conditions of using this website"


@lru_cache(maxsize=None)
def load(name: str) -> str:
    """The scrubbed page as served (UTF-8 text)."""
    return (DIR / name).read_text(encoding="utf-8")
