"""Scrubbed PipelineSuite portal pages and invitation emails for the client
and harvest tests (docs/RFP_PIPELINESUITE.md, section 8).

Captured 2026-09-16 from cgandbinc.pipelinesuite.com and
shfcontracting.pipelinesuite.com over plain httpx with the company's own
invitations, plus the two invitation emails as Graph returned them. What
changed, and nothing else:

- every Security Key is a dummy (`KEY!DUMMY@` for CG&B, `KEY@DUMMY2` for
  SHF), on the emails, in the login page and in the PipelineBid upsell link;
- the confirmation and RFI forms' `cne` (contact id) is `1000001` and the
  `c` (contact token) hidden value is `_dummy_c`;
- every `upn=` tracker token is a short dummy (`u001.DUMMY<n>`), distinct
  per link on the CG&B email so the tests can tell the Yes / No / Unsure
  clicks from the View Files one (`u001.DUMMY2650` in the safelinks href,
  `u001.DUMMY4662` in Outlook's `originalsrc`, `u001.DUMMY7835` on the open
  pixel), and all the same on the SHF text body;
- the Outlook safelinks `data=` / `sdata=` query parts are `DUMMY`.

The project pages, the jstree file lists, the info tables, the notices and
contacts tables and the login form are byte-identical to the capture, so
the parsers and the login chain run over the real shapes.

  cgb_project_377363.html   CG&B project 377363 (Cimarron scoreboard ITB): four
                            flat PDF files, one trade, "Yes" pre-checked, the
                            scope with the CLICK YES / NO / UNSURE banner, an
                            RFI contact named in the scope, Other Info set
  cgb_project_377367.html   CG&B project 377367 (Floyd Edsall gate repairs):
                            seven flat files (one .PDF upper-case), nothing
                            checked, a Notices table (one amendment) and a
                            Project Contacts table (one estimator)
  shf_project_377691.html   SHF project 377691 (Fire Station 95): one folder
                            ("Addendum 01") holding 13 files, .docx among them,
                            extension-less data-text, one file name with a
                            trailing space before .pdf, a Location value
  login_page_with_next.html GET /general/index/next/<token> anonymous: the
                            portalLogin form with the hidden `next` (the
                            token here is the email's own confirmResponse
                            landing, which the client overrides with its own)
  login_page_root.html      GET /general/index: the same form without `next`
  cgb_email_377363.html     the CG&B invitation as Graph renders it (html):
                            the open pixel and the click links, Yes / No /
                            Unsure twice each, View Files twice
  cgb_email_377363.txt      the same email as stored in rfp_emails.body_text
  shf_email_377691.txt      the SHF invitation's body_text
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

DIR = Path(__file__).resolve().parent

CGB_PROJECT_PAGE = "cgb_project_377363.html"
CGB_PROJECT_PAGE_NOTICES = "cgb_project_377367.html"
SHF_PROJECT_PAGE = "shf_project_377691.html"
LOGIN_PAGE_WITH_NEXT = "login_page_with_next.html"
LOGIN_PAGE_ROOT = "login_page_root.html"
CGB_EMAIL_HTML = "cgb_email_377363.html"
CGB_EMAIL_TEXT = "cgb_email_377363.txt"
SHF_EMAIL_TEXT = "shf_email_377691.txt"

ALL_FILES = (
    CGB_PROJECT_PAGE,
    CGB_PROJECT_PAGE_NOTICES,
    SHF_PROJECT_PAGE,
    LOGIN_PAGE_WITH_NEXT,
    LOGIN_PAGE_ROOT,
    CGB_EMAIL_HTML,
    CGB_EMAIL_TEXT,
    SHF_EMAIL_TEXT,
)

CGB_HOST = "cgandbinc.pipelinesuite.com"
CGB_LABEL = "cgandbinc"
CGB_PROJECT_ID = "377363"
CGB_PROJECT_ID_NOTICES = "377367"
CGB_KEY = "KEY!DUMMY@"
CGB_CLIENT_ID = "1090"                      # the opr.pipelinesuite.com client folder

SHF_HOST = "shfcontracting.pipelinesuite.com"
SHF_LABEL = "shfcontracting"
SHF_PROJECT_ID = "377691"
SHF_KEY = "KEY@DUMMY2"
SHF_CLIENT_ID = "1618"

# urlsafe base64 of "ehPipelineSubs/dspProject/projectID/<id>", no padding.
CGB_NEXT_TOKEN = "ZWhQaXBlbGluZVN1YnMvZHNwUHJvamVjdC9wcm9qZWN0SUQvMzc3MzYz"
SHF_NEXT_TOKEN = "ZWhQaXBlbGluZVN1YnMvZHNwUHJvamVjdC9wcm9qZWN0SUQvMzc3Njkx"
# The hidden `next` the captured login page carries (the email's own
# confirmResponse landing), which the client must not post back.
LOGIN_PAGE_NEXT = "ZWhQaXBlbGluZVN1YnMvZHNwUHJvamVjdC9wcm9qZWN0SUQvMzc3MzYzL2NvbmZpcm1SZXNwb25zZS8_"

# The dummy tracker tokens on the CG&B email, by role.
OPEN_PIXEL_TOKEN = "u001.DUMMY7835"
VIEW_FILES_TOKEN = "u001.DUMMY2650"         # the first View Files anchor (safelinks href)
VIEW_FILES_ORIGINALSRC_TOKEN = "u001.DUMMY4662"
RESPONSE_TOKENS = (                         # Yes, No, Unsure (both copies, href + originalsrc)
    "u001.DUMMY1039", "u001.DUMMY3708", "u001.DUMMY773",
    "u001.DUMMY9110", "u001.DUMMY7203", "u001.DUMMY9487",
    "u001.DUMMY5292", "u001.DUMMY102", "u001.DUMMY4112",
    "u001.DUMMY1053", "u001.DUMMY4575", "u001.DUMMY4415",
)
OPEN_PIXEL_URL = f"http://go.pipelinesuite.com/wf/open?upn={OPEN_PIXEL_TOKEN}"
VIEW_FILES_URL = f"http://go.pipelinesuite.com/ls/click?upn={VIEW_FILES_TOKEN}"

# The CG&B project 377363 file names as they sit on the page (flat, no folder).
CGB_FILE_NAMES = (
    "CIMARRON MEMORIAL HS WO1119599 INSTALL SCOREBOARD TO SOCCER FIELD PROJECT MANUAL DWG.pdf",
    "CIMARRON MEMORIAL WO 1119599 PROJECT MANUAL.pdf",
    "ITB.pdf",
    "Specs - Score Board.pdf",
)
CGB_FILE_SIZES_KB = (10082, 6794, 624, 192)
CGB_FILE_IDS = ("10255702", "10255703", "10255704", "10255705")


@lru_cache(maxsize=None)
def load(name: str) -> str:
    """The scrubbed page or body as captured (UTF-8 text)."""
    return (DIR / name).read_text(encoding="utf-8")
