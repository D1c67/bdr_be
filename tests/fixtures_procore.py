"""Procore payloads for the RFP harvest tests (docs/RFP_HARVEST.md).

Trimmed from the live capture of the Warehouse HVAC Upgrade bid sheet
(2026-09-14, company 5662, bid package 1376188, bid 64706611, bid form
11278843). The shape of every payload is the real one; what changed:

- every signed URL is `https://storage.procore.com/api/v5/files/...?sig=test`;
- every phone number is a 555 number and every person is a stand-in with an
  example.com address (the GC's people under monument.example.com, the
  invited recipients under example.com);
- the documents manifest is cut from 72 rows to four: two drawings (two
  disciplines), one specification and one GenericRow.

Everything here is data for `normalize_facts`, `classify_manifest`,
`build_raw` and the MockTransport login flow; nothing is fetched.
"""

from __future__ import annotations

import copy

COMPANY_ID = "5662"
PROJECT_ID = "3642984"
PACKAGE_ID = "1376188"
BID_ID = "64706611"
BID_FORM_ID = "11278843"

SIGNED = "https://storage.procore.com/api/v5/files/us-east-1/pro-core.com/2665-c/4892463-p/{}?companyId=5662&sig=test"

OFFICE_PHONE = "(555) 010-0101"
MOBILE_PHONE = "(555) 010-0102"
GC_PHONE = "(555) 010-0103"
GC_MOBILE = "(555) 010-0104"
VENDOR_PHONE = "(555) 010-0105"
PHONES = (OFFICE_PHONE, MOBILE_PHONE, GC_PHONE, GC_MOBILE, VENDOR_PHONE)

# The Procore reply-to tracking address the bid carries (never stored).
BID_MAILTO = "procore-c49de9e1595195050e997eb12c7237649e3a@procore.example.com"

RECIPIENT_EMAILS = (
    "aupadhyay@example.com",
    "bids@example.com",
    "canderson@example.com",
    "fquintana@example.com",
    "tmoore@example.com",
    "vmadrid@example.com",
)

BID_EMAIL_MESSAGE = (
    '<p><span class="mceitemhidden">Monument Construction invites you to bid.</span></p>\n'
    "<p> <br>Replace evaporative coolers in the warehouse with rooftop air "
    "conditioning units. New electrical service installation. New building envelop "
    "insulation upgrade.<br><br></p>\n"
    '<p><span class="mceitemhidden">Follow the links into our system to download '
    "relevant bidding documents and submit your bids electronically. In this "
    "system, all electronic correspondence is tracked and archived, and bidders are "
    "provided with the most up to date information available for the project.</span></p>"
)
BID_WEB_MESSAGE = (
    "<p>     For help with submitting a bid, please visit Procore's "
    '<a href="https://support.procore.com/products/online/user-guide/project-level/'
    'bidding/tutorials/submit-a-bid">bidding support page</a>.</p>\n'
    "<p> </p>\n"
    "<p>If you need assistance accessing the bidding documents, please email Pat Gale "
    'at Pat<a href="mailto:pat@monument.example.com">@monument.example.com</a>. </p>\n'
    "<p> </p>\n"
    "<p>     Monument looks forward to the opportunity to work with your "
    "project team in our new bidding process.</p>"
)

BID_PACKAGE = {
    "id": 1376188,
    "project_id": 3642984,
    "has_pdm_documents": False,
    "require_nda": False,
    "display_project_name": False,
    "nda_attachments": [],
    "open": True,
    "hidden": False,
    "bid_due_date": "2026-09-10T19:00:00Z",
    "number": 26156,
    "submitted_bids_count": 1,
    "title": "Warehouse HVAC Upgrade",
    "project_name": "Warehouse HVAC Upgrade",
    "project_location": "8250 W Flamingo Road<br>Las Vegas, Nevada 89147<br>United States",
    "accounting_method": "amount",
    "accept_post_due_submissions": False,
    "allow_bidder_sum": False,
    "bid_emails_include_link_to_bidding_documents": True,
    "bid_email_message": BID_EMAIL_MESSAGE,
    "bid_web_message": BID_WEB_MESSAGE,
    "bid_submission_confirmation": (
        "Your bid has successfully been uploaded.  Thank you for working with Monument."
    ),
    "blind_bidding": False,
    "distribution_members": [
        {
            "first": "Robin",
            "last": "Smith",
            "email": "bids@monument.example.com",
            "numbers": f"Office: {OFFICE_PHONE}",
        },
        {"first": "Chloe", "last": "Orwell", "email": "chloe@monument.example.com", "numbers": ""},
    ],
    "created_by": {
        "first": "Chloe",
        "last": "Orwell",
        "email": "chloe@monument.example.com",
        "numbers": "",
    },
    "anticipated_award_date": None,
    "enable_countdown_emails": True,
    "bidding_countdown_email_days": 3,
    "enable_prebid_walkthrough": False,
    "enable_prebid_rfi_deadline": False,
    "distribution_member_ids": [7640119, 15297618],
    "pre_bid_walk_through_date": None,
    "point_of_contact_login_id": 3477184,
    "point_of_contact": {
        "first": "Pat",
        "last": "Gale",
        "email": "pat@monument.example.com",
        "numbers": f"Office: {OFFICE_PHONE}, Mobile: {MOBILE_PHONE}",
    },
    "pre_bid_walk_through_notes": None,
    "pre_bid_rfi_deadline_date": None,
    "project_latitude": 36.1154707,
    "project_longitude": -115.2710105,
    "project_image_url": None,
    "project_logo_name": "Large steps Black on White WITH WEBSITE.jpg",
    "project_logo_url": SIGNED.format("20160414204140_production_375240311.jpg"),
    "sealed": False,
    "links": {
        "analyticsEventsPath": "/rest/v1.0/analytic_events",
        "trades": "/3642984/project/bid_packages/1376188/search_for_bidders/filter_options/trades",
        "bid_list": "/3642984/project/bid_packages/1376188/bidders",
        "bid_packages": "/rest/v1.0/projects/3642984/bid_packages",
        "overview": "/3642984/project/bid_packages/1376188/overview",
        "vendors": "/3642984/project/bid_packages/1376188/vendors",
        "cost_codes": (
            "/3642984/project/bid_packages/1376188/search_for_bidders/filter_options/"
            "standard_cost_codes"
        ),
        "bid_packages_by_project": (
            "/3642984/project/bid_packages/1376188/search_for_bidders/filter_options/"
            "copy_bid_list_from"
        ),
        "submit": "/3642984/project/bidding/bid_packages/1376188/add_to_bid_list",
        "attach_documents": "/3642984/project/bid_packages/1376188/attachments",
        "permission_templates": "/3642984/project/bid_packages/permission_templates",
        "bulk_create_bids": "/3642984/project/bid_packages/1376188/bids/bulk_create",
        "add_vendor": "/3642984/project/bid_packages/1376188/vendors",
    },
    "lump_sum_bidding": False,
    "manager": None,
    "show_bid_info": True,
    "has_any_bid_invited": True,
    "has_bids_sent_nda": False,
    "has_no_nda_activity": True,
    "nda_invited_bids_with_activity_count": 0,
    "attachments_zip_streaming_url": None,
    "bid_form_sections_enabled": True,
    "flexible_response_types_enabled": True,
    "project_currency_iso_code": "USD",
    "project_currency_display": "symbol",
    "lock_unit_fields_base_bid": False,
    "lock_quantity_fields_base_bid": False,
    "lock_unit_fields_alternates": False,
    "lock_quantity_fields_alternates": False,
    "pre_bid_meeting_location": "",
    "pre_bid_meeting_date": None,
    "pre_bid_meeting_online_link": "",
    "pre_bid_meeting_notes": None,
    "public_bid_opening_details_date": None,
    "public_bid_opening_details_location": None,
    "public_bid_opening_details_online_link": None,
    "bid_docs_manifest": {
        "id": 693737582,
        "streaming_url": SIGNED.format("download"),
        "uuid": "85ceb544a3f6856a4ca7f6341fb4372d4e9c6ec250038ba5a29c704b8e704e3c",
    },
}

BID = {
    "id": 64706611,
    "bid_package_id": 1376188,
    "awarded": None,
    "bid_status": "undecided",
    "is_bidder_committed": None,
    "lump_sum_enabled": False,
    "submitted": False,
    "created_at": "2026-09-07T21:57:19Z",
    "updated_at": "2026-09-07T21:58:44Z",
    "show_bid_in_estimating": False,
    "lump_sum_amount": 0,
    "bidder_comments": None,
    "deleted_at": None,
    "recipient_ids": [668733, 4633842, 13002627, 15263129, 16120439, 16171255],
    "recipient_list": [
        {"first": "Abhi", "last": "Upadhyay", "email": "aupadhyay@example.com", "numbers": ""},
        {
            "first": "Thomas",
            "last": "Moore",
            "email": "TMoore@example.com",
            "numbers": f"Office: {OFFICE_PHONE}, Mobile: {MOBILE_PHONE}",
        },
        {"first": "Chance", "last": "Anderson", "email": "canderson@example.com", "numbers": ""},
        {
            "first": "Tiesha",
            "last": "Moore",
            "email": "bids@example.com",
            "numbers": f"Office: {OFFICE_PHONE}, Mobile: {MOBILE_PHONE}",
        },
        {
            "first": "Victoria",
            "last": "Madrid",
            "email": "vmadrid@example.com",
            "numbers": f"Office: {OFFICE_PHONE}",
        },
        {
            "first": "Frank",
            "last": "Quintana",
            "email": "fquintana@example.com",
            "numbers": f"Office: {OFFICE_PHONE}",
        },
    ],
    "recipient_list_with_email_and_number": [
        "Abhi Upadhyay (aupadhyay@example.com)",
        f"Thomas Moore (TMoore@example.com, Office: {OFFICE_PHONE}, Mobile: {MOBILE_PHONE})",
        "Chance Anderson (canderson@example.com)",
        f"Tiesha Moore (bids@example.com, Office: {OFFICE_PHONE}, Mobile: {MOBILE_PHONE})",
        f"Victoria Madrid (vmadrid@example.com, Office: {OFFICE_PHONE})",
        f"Frank Quintana (fquintana@example.com, Office: {OFFICE_PHONE})",
    ],
    "mailto": BID_MAILTO,
    "links": {
        "uploads": "/5662/company/bid/64706611/uploads",
        "cost_codes": "/rest/v1.0/companies/5662/bids/64706611/cost_codes",
        "bid_pdf": "/rest/v1.0/companies/5662/bids/64706611.pdf",
        "nda_mfe_url": (
            "https://app.procore.com/webclients/host/companies/5662/tools/planroom/"
            "bid-packages/1376188/bids/64706611/sign"
        ),
    },
    "bidders_can_add_line_items": True,
    "bid_convertible_to_subcontract": None,
    "bid_convertible_to_purchase_order": None,
    "contract_button_disabled_reason": None,
    "po_button_disabled_reason": None,
    "bid_items": [],
    "attachments": [],
    "bidder_notes": "",
    "attachments_count": 0,
    "bidder_inclusion": None,
    "bidder_exclusion": None,
    "attachments_zip_streaming_url": None,
    "legacy_links": {
        "download_bid_manifest": (
            "https://app.procore.com/5662/company/planroom/download_bid_docs_zip?bid_id=64706611"
        ),
        "email_bid_manifest": (
            "https://app.procore.com/5662/company/planroom/email_bid_docs?bid_id=64706611"
        ),
    },
    "cc_mailto": "pat@monument.example.com",
    "has_bid_docs": True,
    "bid_amount": "",
    "values_converted_by_name": "",
    "values_converted_at": None,
    "values_modified_by_name": "",
    "values_modified_at": None,
    "require_nda": False,
    "bid_package_title": "Warehouse HVAC Upgrade",
    "company_id": 5662,
    "invitation_last_sent_at": "2026-09-07T21:58:44Z",
    "bid_requester": {
        "company": "Monument Construction",
        "contact": f"Pat Gale ({GC_PHONE})",
        "company_address": (
            "<div>Monument Construction<br />7787 Eastgate Road, Unit 110 <br>"
            "Henderson, Nevada 89011<br>United States</div>"
        ),
        "company_phone": GC_PHONE,
        "company_website": "",
        "email_address": "pat@monument.example.com",
        "first_name": "Pat",
        "last_name": "Gale",
        "mobile_phone": GC_MOBILE,
        "vendor_address": "7787 Eastgate Road #110<br>Henderson, Nevada 89011<br>United States",
        "business_phone": GC_PHONE,
        "fax_number": "",
    },
    "bid_form_title": "Warehouse HVAC Upgrade",
    "bid_form_id": 11278843,
    "nda_email_last_sent_at": None,
    "project": {
        "name": "Warehouse HVAC Upgrade",
        "address": "8250 W Flamingo Road<br>Las Vegas, Nevada 89147<br>United States",
    },
    "display_project_name": False,
    "nda_first_name": None,
    "nda_last_name": None,
    "nda_updated_at": None,
    "nda_status": None,
    "nda_signed_at": None,
    "due_date": "2026-09-10T19:00:00Z",
    "vendor": {
        "id": 15767876,
        "name": "G3 Electrical",
        "avatar_url": "",
        "trades": "27 Communications, 26 Electrical",
        "address": "1951 Stella Lake St. #34",
        "business_phone": VENDOR_PHONE,
    },
    "cost_codes": [],
}

BID_FORM = {
    "id": 11278843,
    "title": "Warehouse HVAC Upgrade",
    "position": 1,
    "proposal_id": None,
    "proposal_name": None,
    "lock_unit_fields_base_bid": False,
    "lock_quantity_fields_base_bid": False,
    "lock_unit_fields_alternates": False,
    "lock_quantity_fields_alternates": False,
    "base_bid": [
        {
            "id": 22286426,
            "title": "Base Bid",
            "position": 1,
            "bid_form_items": [
                {
                    "id": 1,
                    "description": "<p>Electrical scope per drawings</p>",
                    "unit": "LS",
                    "quantity": 1,
                    "position": 1,
                    "response_type": "amount",
                },
                {"id": 2, "description": "Rooftop unit power", "position": 2},
            ],
        }
    ],
    "alternates": [{"id": 22286427, "title": None, "position": 1, "bid_form_items": []}],
}

DRAWING_ARCH = {
    "size": 891153,
    "file_path": "Bid_Drawings/Current/Architectural/A7.1-doors-types,-schedule-+-details-Rev.0.pdf",
    "s3_source": SIGNED.format("1787766087_c54655759_p9.pdf"),
    "type": "ZipManifests::BidDocsManifest::DrawingRevisionRow",
    "drawing": {
        "title": "doors types, schedule + details",
        "dpi": 304.76190476190476,
        "revision": "0",
        "drawing_revision_id": 413060808,
        "drawing_set_id": 10439240,
        "drawing_id": 178820045,
        "width": 12800,
        "height": 9144,
        "png_s3_source": SIGNED.format("1787766089_c54655759_p9.png"),
        "thumbnail_url": None,
    },
}
DRAWING_ELEC = {
    "size": 295994,
    "file_path": "Bid_Drawings/Current/Electrical/E4.00-ROOF-ELECTRICAL-PLANS-Rev.0.pdf",
    "s3_source": SIGNED.format("1787766088_c54655759_p28.pdf"),
    "type": "ZipManifests::BidDocsManifest::DrawingRevisionRow",
    "drawing": {
        "title": "ROOF ELECTRICAL PLANS",
        "dpi": 152.38095238095238,
        "revision": "0",
        "drawing_revision_id": 413060816,
        "drawing_set_id": 10439240,
        "drawing_id": 178821040,
        "width": 6400,
        "height": 4571,
        "png_s3_source": SIGNED.format("1787766090_c54655759_p28.png"),
        "thumbnail_url": None,
    },
}
GENERIC_LOG = {
    "size": 2871208,
    "file_path": "Bid_Drawings/Drawing_Log_Current.pdf",
    "s3_source": SIGNED.format("5f7af0e171f028de81211fb785ba9461f22e"),
    "type": "ZipManifests::GenericRow",
}
SPEC_ELEC = {
    "size": 1698010,
    "file_path": (
        "Specifications/26-Electrical/26-28-16-Enclosed-Switches-and-Circuit-Breakers_Rev_0.pdf"
    ),
    "s3_source": SIGNED.format("bfe5befe-b221-417c-9ec5-d5164451b674_production_merged.pdf"),
    "type": "ZipManifests::SpecificationsManifest::SpecificationSectionRevisionRow",
}

DOCUMENTS = {
    "id": 1376188,
    "title": "Warehouse HVAC Upgrade",
    "files": [DRAWING_ARCH, DRAWING_ELEC, GENERIC_LOG, SPEC_ELEC],
}

MANIFEST_URLS = [row["s3_source"] for row in DOCUMENTS["files"]]


def bid_package() -> dict:
    return copy.deepcopy(BID_PACKAGE)


def bid() -> dict:
    return copy.deepcopy(BID)


def bid_form() -> dict:
    return copy.deepcopy(BID_FORM)


def documents() -> dict:
    return copy.deepcopy(DOCUMENTS)


# ── The invitation email body (Outlook-rewritten, as stored in body_text) ──

SAFELINK = "https://nam09.safelinks.protection.outlook.com/?url={}&data=05%7C02%7Ctest&reserved=0"


def _safelink(url: str) -> str:
    from urllib.parse import quote

    return SAFELINK.format(quote(url, safe=""))


ROUTE_URL = f"https://app.procore.com/{COMPANY_ID}/company/planroom/route_to_bid_sheet/{BID_ID}?from_email=invitation"
ZIP_URL = (
    f"https://app.procore.com/{COMPANY_ID}/company/planroom/download_zip?bid_id={BID_ID}"
    "&zip_manifest_uuid=0f6c8fd576f79a9babf3044b58ff52deba8996b53522d4fc87af727dd3de7d16"
)
INTENT_WILL_BID = (
    f"https://app.procore.com/{PROJECT_ID}/project/public/bid/{BID_ID}/intents/"
    "public_set_bid_intent?intent=will_bid&token=abc123"
)
INTENT_WILL_NOT_BID = (
    f"https://app.procore.com/{PROJECT_ID}/project/public/bid/{BID_ID}/intents/"
    "public_set_bid_intent?intent=will_not_bid&token=abc123"
)

EMAIL_BODY = f"""Monument Construction invites you to bid.

Bid Package: Warehouse HVAC Upgrade
Bid Due: September 10, 2026 12:00 PM PDT

Will Bid <{_safelink(INTENT_WILL_BID)}>
Will Not Bid <{_safelink(INTENT_WILL_NOT_BID)}>

View Bid Sheet <{_safelink(ROUTE_URL)}>
Download Bidding Documents <{_safelink(ZIP_URL)}>

Replace evaporative coolers in the warehouse with rooftop air conditioning units.
"""

# The login pages, as the Rails forms render them (captured 2026-09-14).
EMAIL_TOKEN = "M1JLDEEbL17oi9Nzje2kJW2qorz1KvR3UfSrQDGu_D2pphZ61GutFSqQ9ydmwqHDhFirzHmn30R7O0p1aXnoFQ"
PASSWORD_TOKEN = "Zq8Lk2Pw9Xn4Vb6Hd1Tf3Gs5Jr7Mc0Ye2Ua4Ib6Od8Ep1Wq3Rt5Yu7Io9Pa1Sd3Fg5Hj7Kl9Zx2Cv4Bn6"

EMAIL_PAGE = f"""<!DOCTYPE html><html><head><title>Procore Log In</title></head><body>
<form class="form form--session" data-continue="Continue" data-submit="Log In" data-forgot-password-url="/passwords/new" action="/sessions/submit_login_email" accept-charset="UTF-8" method="post">
<input type="hidden" name="authenticity_token" value="{EMAIL_TOKEN}" autocomplete="off" />
<input autocomplete="off" type="hidden" name="session[sso_target_url]" id="session_sso_target_url" />
<input autocomplete="username" autofocus="autofocus" type="email" name="session[email]" id="session_email" />
<input type="checkbox" value="true" name="session[remember_me]" id="session_remember_me" />
<button type="submit">Continue</button>
</form></body></html>"""

PASSWORD_PAGE = f"""<!DOCTYPE html><html><head><title>Procore Log In</title></head><body>
<form class="form form--session" action="/sessions/submit_login_password" accept-charset="UTF-8" method="post">
<input type="hidden" name="authenticity_token" value="{PASSWORD_TOKEN}" autocomplete="off" />
<input type="hidden" name="session[login_step]" value="password" />
<input autocomplete="current-password" type="password" name="session[password]" id="session_password" />
<button type="submit">Log In</button>
</form></body></html>"""

EMAIL_REJECTED_PAGE = EMAIL_PAGE.replace(
    "<form ", '<div class="flash flash--error">We could not find an account for that email.</div>\n<form ', 1
)

CHALLENGE_PAGE = """<!DOCTYPE html><html><head><title>Just a moment...</title>
<script src="/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1?ray=abc"></script>
</head><body><div id="challenge-running">Checking your browser before accessing app.procore.com.</div></body></html>"""
