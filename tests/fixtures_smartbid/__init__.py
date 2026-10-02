"""Scrubbed SmartBid API answers and invitation emails for the client and
harvest tests (docs/RFP_SMARTBID.md, section 8).

Captured 2026-10-01 over plain httpx with the company's own invitations
(DC Building Group 874974, Martin-Harris 876398 and 876488, R&O 874512),
plus three invitation emails as rfp_emails.body_text stores them and the
874974 email as Graph renders it (html). What changed, and nothing else:

- every 40-hex passport key is `DEADBEEF` x 4 + `00<bid project id>`, in
  the links (safelinks-wrapped and Outlook's `originalsrc`) and in each
  `BidProject.PassportKey`;
- every 15-hex access key is `abcdef000<bid project id>`;
- every comm detail id (`cId=bp_<id>`, `sCommunicationId`, the Unsubscribe
  `DId`, the anchors' `id` suffixes) is `1000<bid project id>`, and every
  person id (`PId`, `BidProject.PersonId`) is `2000000<n>`;
- the SendGrid `upn=` token is `u001.DUMMYOPEN874974`;
- the Outlook safelinks `data=` / `sdata=` query parts are `DUMMY`;
- the SAS `sig=` in the direct-URL answer is `DUMMYSIGNATURE%3D`;
- the `getbidproject` answers lost `sPlanRoomString` (the plan room again,
  as HTML) and `Integration` (cloud-sync settings), to shrink them.

The plan-room trees, the facts, the invitations and the email wording are
as captured (the JSON re-serialized compactly, values unchanged), so the
parsers run over the real shapes.

  bp_874974.json   DC Building Group, NSU @ NLV Gateway: 45 files in nested
                   folders (two levels under "RFI's" and "Shell Bid Set "),
                   .docx and .xlsx among them, folder names with trailing
                   spaces, TimeZoneShort (CT), one code "Accepted"
  bp_876398.json   Martin-Harris, UNLV Dental: 7 files in two flat folders,
                   (PT), the code still "Invited"
  bp_874512.json   R&O, NSU Gateway Site 3: 19 files, addenda two levels deep
  bp_876488.json   Martin-Harris, Whole Foods: 6 files, two levels
  ca_<bid>.json    the four captured getconfidentialagreement answers (open)
  ca_agreement_general.json            synthetic: a general agreement owed
  ca_agreement_general_accepted.json   synthetic: the same, accepted (StatusCA 1)
  ca_agreement_specific.json           synthetic: a specific agreement owed
  ca_pad.json                          synthetic: a PAD invitation row
  ca_not_allowed.json                  synthetic: an AlowedDetail sentence
  token.json       a /token answer with a dummy bearer token
  direct.txt       a direct-URL answer (the 874974 Exhibit F .docx blob URL)
  email_874974.txt / .html, email_876398.txt, email_876488.txt   the emails
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

DIR = Path(__file__).resolve().parent

BP_874974 = "bp_874974.json"
BP_876398 = "bp_876398.json"
BP_874512 = "bp_874512.json"
BP_876488 = "bp_876488.json"
CA_OPEN = ("ca_874974.json", "ca_876398.json", "ca_874512.json", "ca_876488.json")
CA_GENERAL = "ca_agreement_general.json"
CA_GENERAL_ACCEPTED = "ca_agreement_general_accepted.json"
CA_SPECIFIC = "ca_agreement_specific.json"
CA_PAD = "ca_pad.json"
CA_NOT_ALLOWED = "ca_not_allowed.json"
TOKEN_ANSWER = "token.json"
DIRECT_URL_ANSWER = "direct.txt"
EMAIL_874974_TEXT = "email_874974.txt"
EMAIL_874974_HTML = "email_874974.html"
EMAIL_876398_TEXT = "email_876398.txt"
EMAIL_876488_TEXT = "email_876488.txt"

ALL_FILES = (
    BP_874974, BP_876398, BP_874512, BP_876488, *CA_OPEN,
    CA_GENERAL, CA_GENERAL_ACCEPTED, CA_SPECIFIC, CA_PAD, CA_NOT_ALLOWED,
    TOKEN_ANSWER, DIRECT_URL_ANSWER,
    EMAIL_874974_TEXT, EMAIL_874974_HTML, EMAIL_876398_TEXT, EMAIL_876488_TEXT,
)


def key_for(bid: str) -> str:
    """The dummy passport key the fixtures carry for a bid project."""
    return f"DEADBEEFDEADBEEFDEADBEEFDEADBEEF00{bid}"


def comm_for(bid: str) -> str:
    """The dummy comm detail id the fixtures carry for a bid project."""
    return f"1000{bid}"


BID = "874974"
KEY = key_for(BID)
COMM = comm_for(BID)
SYSTEM_ID = 3766
ACCESS_KEY = "abcdef000874974"
ACCOUNT_ID = "20000001"

# The bearer token in token.json, and the per-file security token and SAS
# signature the tests' fake SmartBid hands out (none of these is real).
BEARER = "DUMMY-BEARER-TOKEN-" + "x" * 64
SECURITY_TOKEN = "DUMMYSECTOKEN+abc/def=="
SAS_SIGNATURE = "DUMMYSIGNATURE%3D"

# The links the 874974 email carries, unwrapped.
VIEW_URL = (
    f"https://securecc.smartbidnet.com/Main/Login.aspx?cId=bp_{COMM}&sPassportKey={KEY}"
    f"&sBidId={BID}&st=105&e=1"
)
IMAGE_LINK_URL = VIEW_URL.replace("st=105", "st=101")
YES_URL = VIEW_URL.replace("&st=105", "&iR=1&st=103")
NO_URL = VIEW_URL.replace("&st=105", "&iR=0&st=104")
BIDBOARD_URL = (
    f"https://securecc.smartbidnet.com/External/ViewOnDigitalBidBoard.aspx?cId=bp_{COMM}"
    f"&sPassportKey={KEY}&sBidId={BID}&st=116&e=1"
)
READ_RECEIPT_URL = (
    f"https://securecc.smartbidnet.com/External/RequestReadReceipt.aspx?sCommunicationId={COMM}"
    "&oimg=1x1pic.gif"
)
OPEN_PIXEL_URL = "http://em.smartinsight.co/wf/open?upn=u001.DUMMYOPEN874974"
UNSUBSCRIBE_URL = (
    f"https://securecc.smartbidnet.com/External/Unsubscribe.aspx?DId={COMM}&PId=20000001"
    "&CType=1&st=106&e=1"
)

# 874974's Exhibit F (the file direct.txt points at).
EXHIBIT_F_ID = "53529895"
EXHIBIT_F_NAME = "Exhibit_F_DC_Building_Insurance_Requirements.docx"
EXHIBIT_F_VALUE = "NTM1Mjk4OTUuODc0OTc0LjM3NjY="


@lru_cache(maxsize=None)
def load(name: str) -> str:
    """The scrubbed answer or body as captured (UTF-8 text)."""
    return (DIR / name).read_text(encoding="utf-8")


def load_json(name: str):
    """A fresh parse of a JSON fixture (callers may mutate it)."""
    return json.loads(load(name))
