"""cloud_folders: share-link detection over the exact URL shapes captured on
dev (scratchpad probe_facts.md, 2026-09-16), each provider's resolve path
against a fake of the live responses (SharePoint redeem + FedAuth + REST
listing, Dropbox folder zip, Google Drive file confirm / API / embedded view,
Box file vs folder, the four hands-off providers), the SSRF guards (per-hop
allowlist, https only, cookies to the SharePoint host only), and the
downloader's caps and error mapping. Everything runs on httpx.MockTransport;
no test touches the network."""

from __future__ import annotations

import io
import json
import zipfile
from urllib.parse import parse_qs, quote, unquote, urlparse

import httpx
import pytest

from app.core.config import Settings
from app.services import cloud_folders as cf
from app.services.cloud_folders import (
    CloudForbidden,
    CloudTransient,
    Listing,
    RemoteFile,
    ShareLink,
    find_share_links,
    link_from_url,
)

PDF = b"%PDF-1.7\n" + b"x" * 3000 + b"\n%%EOF\n"

SP_HOST = "eagle1lv.sharepoint.com"
SP_SHARE = f"https://{SP_HOST}/:f:/s/EOCExternal/IgBq0EfOK_k3RLqNQmoQ7ffbAWer_cGJ-U2o9utlWTMpSoo?e=SbFLil"
SP_FOLDER = "/sites/EOCExternal/Shared Documents/26-080 UMC MLK Warehouse Remodel"
SP_LANDING = (
    f"https://{SP_HOST}/sites/EOCExternal/Shared%20Documents/Forms/AllItems.aspx"
    "?id=%2Fsites%2FEOCExternal%2FShared%20Documents%2F26%2D080%20UMC%20MLK%20Warehouse%20Remodel&p=true&ga=1"
)
ONEDRIVE_SHARE = "https://g3electrical-my.sharepoint.com/:f:/p/t_moorejr/IgADezL9BWiKSK-Cc8YrBkNMAf6O1h4U_1l83ps6WDsw9FQ"
DROPBOX_FOLDER = (
    "https://www.dropbox.com/scl/fo/cv15gi89jjt514by214om/AHvlRf6u0-J2cRcsqe9n4R8"
    "?rlkey=vt6nc04tsnd0b9b25kb3plckk&dl=0"
)
DROPBOX_FOLDER_SAFELINKS = (
    "https://nam09.safelinks.protection.outlook.com/?url=https%3A%2F%2Fwww.dropbox.com%2Fscl%2Ffo"
    "%2Fcv15gi89jjt514by214om%2FAHvlRf6u0-J2cRcsqe9n4R8%3Frlkey%3Dvt6nc04tsnd0b9b25kb3plckk%26dl%3D0"
    "&data=05%7C02%7Cx&reserved=0"
)


def _settings(**over):
    base = dict(rfp_ingest_enabled=True, rfp_harvest_enabled=True)
    base.update(over)
    return Settings(_env_file=None, **base)


@pytest.fixture(autouse=True)
def _clean_jars():
    cf.forget_jars()
    yield
    cf.forget_jars()


class Recorder:
    """A MockTransport handler that records every request and delegates to
    `route(request)`; raising inside `route` (a transport error) propagates."""

    def __init__(self, route):
        self.route = route
        self.requests: list[httpx.Request] = []

    def __call__(self, request):
        self.requests.append(request)
        return self.route(request)

    @property
    def hosts(self):
        return [r.url.host for r in self.requests]

    def cookies_sent_to(self, host):
        return [r.headers.get("cookie") for r in self.requests if r.url.host == host]


def _mock(monkeypatch, route) -> Recorder:
    rec = Recorder(route)
    monkeypatch.setattr(
        cf, "_client",
        lambda timeout: httpx.Client(transport=httpx.MockTransport(rec), follow_redirects=False),
    )
    return rec


def _redirect(location, **headers):
    return httpx.Response(302, headers={"location": location, **headers})


def _html(body="<html><body>page</body></html>", status=200):
    return httpx.Response(status, text=body, headers={"content-type": "text/html; charset=utf-8"})


def _json(data, status=200):
    return httpx.Response(status, content=json.dumps(data).encode(),
                          headers={"content-type": "application/json;odata=nometadata"})


def _pdf(name="file.pdf", body=PDF, **headers):
    return httpx.Response(200, content=body, headers={
        "content-type": "application/octet-stream",
        "content-disposition": f'attachment; filename="{name}"',
        **headers,
    })


def _refuse(request):
    raise AssertionError(f"no request expected, got {request.url}")


# ── find_share_links over the dev shapes ─────────────────────────────────


PROBE_TEXT = f"""
Please find the bid documents here: {SP_SHARE}
Also: {SP_SHARE}&xsdata=MDV8MDJ8abc&sdata=eGhj
Personal: {ONEDRIVE_SHARE}
Dropbox: {DROPBOX_FOLDER}
Wrapped: {DROPBOX_FOLDER_SAFELINKS}
ShareFile: https://sletteninc.sharefile.com/d-s5272283033994a1b9120d20ad63b4fff
ShareFile 2: https://sletteninc.sharefile.com/public/share/web-s5030aec135b14046a3c2b01282109eb8
Ignore: https://aka.ms/LearnAboutSenderIdentification https://www.eocelectric.com/projects
https://www.linkedin.com/company/eoc https://linkprotect.cudasvc.com/url?a=https%3a%2f%2fx.com&c=E,1
https://ci3.googleusercontent.com/meips/ADKq_Nb/sig.png
"""


def test_finds_the_dev_link_shapes_and_ignores_the_rest():
    links = find_share_links(PROBE_TEXT, None)
    assert [(x.provider, x.kind, x.supported) for x in links] == [
        ("sharepoint", "folder", True),
        ("onedrive", "folder", True),
        ("dropbox", "folder", True),
        ("sharefile", "unknown", False),
        ("sharefile", "unknown", False),
    ]
    sp, od, db, sf1, sf2 = links
    assert sp.key == f"sharepoint:{SP_HOST}:IgBq0EfOK_k3RLqNQmoQ7ffbAWer_cGJ-U2o9utlWTMpSoo"
    # Outlook decoration (e=, xsdata, sdata) is gone from the stored URL.
    assert sp.url == f"https://{SP_HOST}/:f:/s/EOCExternal/IgBq0EfOK_k3RLqNQmoQ7ffbAWer_cGJ-U2o9utlWTMpSoo"
    assert od.key == "sharepoint:g3electrical-my.sharepoint.com:IgADezL9BWiKSK-Cc8YrBkNMAf6O1h4U_1l83ps6WDsw9FQ"
    assert db.key == "dropbox:cv15gi89jjt514by214om:vt6nc04tsnd0b9b25kb3plckk"
    assert db.url == DROPBOX_FOLDER
    assert sf1.key == "sharefile:sletteninc.sharefile.com:d-s5272283033994a1b9120d20ad63b4fff"
    assert sf2.key == "sharefile:sletteninc.sharefile.com:web-s5030aec135b14046a3c2b01282109eb8"


def test_xsdata_and_safelinks_twins_dedup_to_the_bare_key():
    bare = link_from_url(SP_SHARE)
    decorated = link_from_url(SP_SHARE + "&xsdata=MDV8MDJ8abc&sdata=eGhj")
    assert bare.key == decorated.key and bare.url == decorated.url
    plain = link_from_url(DROPBOX_FOLDER)
    wrapped = link_from_url(DROPBOX_FOLDER_SAFELINKS)
    assert plain.key == wrapped.key and wrapped.url == DROPBOX_FOLDER


def test_html_anchor_prefers_originalsrc_and_keeps_the_label():
    html = (
        '<a href="https://nam09.safelinks.protection.outlook.com/?url=https%3A%2F%2Fstarkecontractors-my'
        '.sharepoint.com%2F%3Af%3A%2Fp%2Fjoe%2FTOKEN1%3Fe%3Dabc&amp;data=05%7C02&amp;reserved=0"'
        ' originalsrc="https://starkecontractors-my.sharepoint.com/:f:/p/joe/TOKEN1?e=abc">'
        "Bid Documents</a>"
        '<a href="https://www.dropbox.com/scl/fi/abc123/Spec%20Book.pdf?rlkey=k1&amp;dl=0">'
        "https://www.dropbox.com/scl/fi/abc123/Spec%20Book.pdf?rlkey=k1&amp;dl=0</a>"
    )
    links = find_share_links(None, html)
    assert [(x.provider, x.kind, x.label) for x in links] == [
        ("onedrive", "folder", "Bid Documents"),
        ("dropbox", "file", ""),              # anchor text that is the URL is no label
    ]
    assert links[0].url == "https://starkecontractors-my.sharepoint.com/:f:/p/joe/TOKEN1"
    assert links[1].url == "https://www.dropbox.com/scl/fi/abc123/Spec%20Book.pdf?rlkey=k1&dl=0"


def test_label_rules_first_wins_and_non_empty_beats_empty():
    html = f'<a href="{SP_SHARE}">Folder A</a> <a href="{SP_SHARE}&xsdata=1">Folder B</a>'
    assert [x.label for x in find_share_links(None, html)] == ["Folder A"]
    # Bare in the text first, labelled in the HTML: the label still lands.
    links = find_share_links(SP_SHARE, f'<a href="{SP_SHARE}">Bid Docs</a>')
    assert len(links) == 1 and links[0].label == "Bid Docs"


def test_recognises_every_provider_shape():
    cases = {
        "https://drive.google.com/drive/folders/1AbC_dEf-9?usp=sharing": ("gdrive", "folder", "gdrive:1AbC_dEf-9", True),
        "https://drive.google.com/drive/u/0/folders/1AbC_dEf-9": ("gdrive", "folder", "gdrive:1AbC_dEf-9", True),
        "https://drive.google.com/file/d/1FiLe/view?usp=drive_link": ("gdrive", "file", "gdrive:1FiLe", True),
        "https://drive.google.com/open?id=1OpEn": ("gdrive", "file", "gdrive:1OpEn", True),
        "https://docs.google.com/spreadsheets/d/1ShEeT/edit#gid=0": ("gdrive", "file", "gdrive:1ShEeT", False),
        "https://docs.google.com/document/d/1DoC/edit": ("gdrive", "file", "gdrive:1DoC", False),
        "https://app.box.com/s/abc123XYZ": ("box", "unknown", "box:abc123XYZ", True),
        "https://gc.app.box.com/s/abc123XYZ": ("box", "unknown", "box:abc123XYZ", True),
        "https://www.dropbox.com/sh/tok3n/AABBcc?dl=0": ("dropbox", "folder", "dropbox:tok3n", True),
        "https://www.dropbox.com/s/tok3n/quote.pdf?dl=0": ("dropbox", "file", "dropbox:tok3n", True),
        "https://www.dropbox.com/scl/fi/fid/Spec.pdf?rlkey=rk&dl=0": ("dropbox", "file", "dropbox:fid:rk", True),
        "https://1drv.ms/f/s!AbCdEf123": ("onedrive", "unknown", "sharepoint:1drv.ms:s!AbCdEf123", True),
        "https://1drv.ms/b/c/1234/EaBc?e=xyz": ("onedrive", "unknown", "sharepoint:1drv.ms:EaBc", True),
        "https://tenant.sharepoint.com/:b:/s/Site/EbCd?e=1": ("sharepoint", "file", "sharepoint:tenant.sharepoint.com:EbCd", True),
        "https://tenant.sharepoint.com/:x:/s/Site/ExCe": ("sharepoint", "file", "sharepoint:tenant.sharepoint.com:ExCe", True),
        "https://tenant-my.sharepoint.com/:f:/g/personal/j_x_com/EfGh?csf=1&web=1&e=q": ("onedrive", "folder", "sharepoint:tenant-my.sharepoint.com:EfGh", True),
        "https://gc.egnyte.com/fl/abc123": ("egnyte", "unknown", "egnyte:gc.egnyte.com:/fl/abc123", False),
        "https://we.tl/t-AbC123": ("wetransfer", "unknown", "wetransfer:/t-AbC123", False),
        "https://wetransfer.com/downloads/abc/def": ("wetransfer", "unknown", "wetransfer:/downloads/abc/def", False),
        "https://spaces.hightail.com/space/AbC": ("hightail", "unknown", "hightail:/space/AbC", False),
    }
    for url, expected in cases.items():
        link = link_from_url(url)
        assert link is not None, url
        assert (link.provider, link.kind, link.key, link.supported) == expected, url
    assert link_from_url("https://tenant-my.sharepoint.com/:f:/g/personal/j_x_com/EfGh?csf=1&web=1&e=q").url == (
        "https://tenant-my.sharepoint.com/:f:/g/personal/j_x_com/EfGh?csf=1&web=1"
    )


@pytest.mark.parametrize("url", [
    "http://www.dropbox.com/scl/fo/x/y?rlkey=k",     # not https
    "https://www.dropbox.com/login",
    "https://www.dropbox.com/",
    "https://drive.google.com/drive/my-drive",
    "https://app.box.com/folder/123",
    "https://tenant.sharepoint.com/",
    "https://sletteninc.sharefile.com/",
    "https://example.com/:f:/s/Site/Tok",
    "https://evil.sharepoint.com.attacker.example/:f:/s/Site/Tok",
])
def test_ignores_non_share_urls(url):
    assert link_from_url(url) is None


def test_trailing_punctuation_in_prose_is_stripped():
    links = find_share_links(f"See {DROPBOX_FOLDER}. Thanks!", None)
    assert len(links) == 1 and links[0].url == DROPBOX_FOLDER


def test_empty_inputs_and_the_link_cap():
    assert find_share_links(None, None) == []
    assert find_share_links("", "") == []
    text = "\n".join(f"https://tenant.sharepoint.com/:f:/s/S/TOK{i}" for i in range(cf._MAX_LINKS + 40))
    assert len(find_share_links(text, None)) == cf._MAX_LINKS


def test_hostile_html_never_breaks_detection():
    assert find_share_links(None, "<a href=" * 5000 + f'<a href="{SP_SHARE}">x</a>') != []


# ── SharePoint ───────────────────────────────────────────────────────────


class FakeSharePoint:
    """The captured EOC share: the share URL 302s with FedAuth onto the
    library view; with the cookie, the REST folder listing (Length as a
    string, a subfolder without ServerRelativeUrl) and file bytes answer;
    without it, 403. Modes arm the sign-in and failure shapes."""

    def __init__(self):
        self.mode = "ok"
        self.tree = {
            SP_FOLDER: {
                "Files": [
                    ("Exhibit A - Scope of Work.pdf", "401478"),
                    ("Reference Material - Architectural - Rev2-FullSet.pdf", "18196971"),
                ],
                "Folders": [("Addendum 2", True), ("RFI Addendum", False)],
            },
            SP_FOLDER + "/Addendum 2": {"Files": [("E-101.pdf", "1000")], "Folders": [("Deep", True)]},
            SP_FOLDER + "/Addendum 2/Deep": {"Files": [("Deeper.pdf", "5")], "Folders": []},
            SP_FOLDER + "/RFI Addendum": {"Files": [("It's RFI-1.pdf", "77")], "Folders": []},
        }

    def _authed(self, request):
        return "FedAuth=guest-token" in (request.headers.get("cookie") or "")

    def __call__(self, request):
        path = unquote(request.url.path)
        if request.url.host != SP_HOST:
            raise AssertionError(f"unexpected host {request.url.host}")
        if path.startswith("/:"):
            if self.mode == "login":
                return _redirect("https://login.microsoftonline.com/common/oauth2/authorize?x=1")
            if self.mode == "authenticate":
                return _redirect(f"https://{SP_HOST}/sites/EOCExternal/_layouts/15/authenticate.aspx?Source=x")
            if self.mode == "accessdenied":
                return _redirect(f"https://{SP_HOST}/_layouts/15/accessdenied.aspx?x=1")
            if self.mode == "403":
                return httpx.Response(403)
            if self.mode == "503":
                return httpx.Response(503)
            if self.mode == "offsite":
                return _redirect("https://169.254.169.254/latest/meta-data/")
            if self.mode == "loop":
                return _redirect(f"https://{SP_HOST}/:f:/s/EOCExternal/again")
            if self.mode == "transport":
                raise httpx.ReadTimeout("boom", request=request)
            if request.url.params.get("download") == "1":
                if not self._authed(request):
                    return httpx.Response(403)
                return _pdf("Exhibit B.pdf", body=PDF, **{"content-length": str(len(PDF))})
            if self.mode == "noid":
                return _redirect(f"https://{SP_HOST}/sites/EOCExternal/_layouts/15/Doc.aspx?sourcedoc=%7Bguid%7D&file=x.pdf", **{
                    "set-cookie": "FedAuth=guest-token; path=/; SameSite=None; secure; HttpOnly",
                })
            landing = SP_LANDING
            if self.mode == "file":
                landing = (
                    f"https://{SP_HOST}/sites/EOCExternal/Shared%20Documents/Forms/AllItems.aspx"
                    "?id=%2Fsites%2FEOCExternal%2FShared%20Documents%2F26%2D080%2FExhibit%20A%20%2D%20Scope%20of%20Work.pdf"
                    "&parent=%2Fsites%2FEOCExternal%2FShared%20Documents%2F26%2D080"
                )
            return _redirect(landing, **{
                "set-cookie": "FedAuth=guest-token; path=/; SameSite=None; secure; HttpOnly",
            })
        if path.endswith("/Forms/AllItems.aspx") or "/_layouts/15/" in path:
            return _html("<html>" + "x" * 1000 + "</html>")
        if "/_api/web/GetFolderByServerRelativeUrl(" in path:
            if not self._authed(request):
                return httpx.Response(403)
            if self.mode == "api-html":
                return _html("<html>Sign in</html>")
            assert request.headers["accept"] == "application/json;odata=nometadata"
            assert request.url.params["$expand"] == "Folders,Files"
            folder = path.split("GetFolderByServerRelativeUrl('", 1)[1].rsplit("')", 1)[0].replace("''", "'")
            node = self.tree.get(folder)
            if node is None:
                return httpx.Response(500, content=b'{"odata.error":{"code":"-2147024894"}}')
            return _json({
                "Name": folder.rsplit("/", 1)[-1],
                "ItemCount": len(node["Files"]) + len(node["Folders"]),
                "Files": [
                    {"Name": n, "Length": length, "ServerRelativeUrl": f"{folder}/{n}"}
                    for n, length in node["Files"]
                ],
                "Folders": [
                    {"Name": n, "ServerRelativeUrl": f"{folder}/{n}"} if with_url else {"Name": n}
                    for n, with_url in node["Folders"]
                ],
            })
        if "/_api/web/GetFileByServerRelativeUrl(" in path:
            if not self._authed(request):
                return httpx.Response(403)
            literal = path.split("GetFileByServerRelativeUrl('", 1)[1].rsplit("')", 1)[0].replace("''", "'")
            if path.endswith("/$value"):
                return httpx.Response(200, content=PDF, headers={
                    "content-type": "application/octet-stream", "content-disposition": "attachment",
                })
            if literal.endswith(".pdf"):
                return _json({"Name": literal.rsplit("/", 1)[-1], "Length": "401478"})
            return httpx.Response(500, content=b'{"odata.error":{"code":"-2147024894"}}')
        raise AssertionError(f"unrouted {request.url}")


@pytest.fixture
def sp(monkeypatch):
    fake = FakeSharePoint()
    rec = _mock(monkeypatch, fake)
    rec.fake = fake
    return rec


def test_sharepoint_folder_lists_recursively_with_the_guest_jar(sp, tmp_path):
    link = link_from_url(SP_SHARE)
    listing = cf.resolve(link, tmp_path, _settings())
    assert listing.status == "listed" and listing.error is None and not listing.truncated
    assert [(f.path, f.size) for f in listing.files] == [
        ("Exhibit A - Scope of Work.pdf", 401478),
        ("Reference Material - Architectural - Rev2-FullSet.pdf", 18196971),
        ("Addendum 2/E-101.pdf", 1000),
        ("RFI Addendum/It's RFI-1.pdf", 77),
        ("Addendum 2/Deep/Deeper.pdf", 5),
    ]
    loc = listing.files[3].locator
    assert loc.startswith(f"https://{SP_HOST}/sites/EOCExternal/_api/web/GetFileByServerRelativeUrl('")
    assert loc.endswith("/$value")
    # Single quotes doubled inside the literal, then percent-encoded like the rest.
    assert "It%27%27s%20RFI-1.pdf" in loc
    # Redeem, landing page, then one listing call per folder (no page bytes read).
    assert [unquote(r.url.path) for r in sp.requests] == [
        "/:f:/s/EOCExternal/IgBq0EfOK_k3RLqNQmoQ7ffbAWer_cGJ-U2o9utlWTMpSoo",
        "/sites/EOCExternal/Shared Documents/Forms/AllItems.aspx",
        f"/sites/EOCExternal/_api/web/GetFolderByServerRelativeUrl('{SP_FOLDER}')",
        f"/sites/EOCExternal/_api/web/GetFolderByServerRelativeUrl('{SP_FOLDER}/Addendum 2')",
        f"/sites/EOCExternal/_api/web/GetFolderByServerRelativeUrl('{SP_FOLDER}/RFI Addendum')",
        f"/sites/EOCExternal/_api/web/GetFolderByServerRelativeUrl('{SP_FOLDER}/Addendum 2/Deep')",
    ]
    assert sp.requests[0].url.params.get("e") is None      # decoration never sent
    assert "cookie" not in sp.requests[0].headers
    assert all("FedAuth=guest-token" in r.headers["cookie"] for r in sp.requests[1:])
    # The jar is parked for download.
    assert cf._jar_for(SP_HOST, SP_FOLDER + "/Addendum 2/E-101.pdf") is not None


def test_sharepoint_download_presents_the_parked_jar(sp, tmp_path):
    listing = cf.resolve(link_from_url(SP_SHARE), tmp_path, _settings())
    target = listing.files[3]
    dest = tmp_path / "rfi.pdf"
    written = cf.download(target.locator, dest, max_bytes=len(PDF), timeout=5)
    assert written == len(PDF) and dest.read_bytes() == PDF
    last = sp.requests[-1]
    assert unquote(last.url.path).endswith("/RFI Addendum/It''s RFI-1.pdf')/$value")
    assert "FedAuth=guest-token" in last.headers["cookie"]


def test_sharepoint_download_without_a_jar_is_forbidden(sp, tmp_path):
    listing = cf.resolve(link_from_url(SP_SHARE), tmp_path, _settings())
    cf.forget_jars()
    dest = tmp_path / "x.pdf"
    with pytest.raises(CloudForbidden):
        cf.download(listing.files[0].locator, dest, max_bytes=10_000, timeout=5)
    assert "cookie" not in sp.requests[-1].headers and not dest.exists()


def test_sharepoint_depth_cap_truncates(sp, tmp_path):
    listing = cf.resolve(link_from_url(SP_SHARE), tmp_path, _settings(rfp_harvest_folder_max_depth=1))
    assert listing.status == "listed" and listing.truncated
    assert [f.path for f in listing.files] == [
        "Exhibit A - Scope of Work.pdf",
        "Reference Material - Architectural - Rev2-FullSet.pdf",
        "Addendum 2/E-101.pdf",
        "RFI Addendum/It's RFI-1.pdf",
    ]
    assert not any("Deep" in unquote(r.url.path) for r in sp.requests)
    listing = cf.resolve(link_from_url(SP_SHARE), tmp_path, _settings(rfp_harvest_folder_max_depth=0))
    assert listing.truncated and len(listing.files) == 2


def test_sharepoint_file_cap_top_level_is_too_many_else_truncates(sp, tmp_path):
    listing = cf.resolve(link_from_url(SP_SHARE), tmp_path, _settings(rfp_harvest_max_files=1))
    assert listing.status == "too_many_files" and listing.files == [] and "more than 1 files" in listing.error
    listing = cf.resolve(link_from_url(SP_SHARE), tmp_path, _settings(rfp_harvest_max_files=3))
    assert listing.status == "listed" and listing.truncated
    assert len(listing.files) == 3


@pytest.mark.parametrize("mode", ["login", "authenticate", "accessdenied", "403"])
def test_sharepoint_sign_in_walls(sp, tmp_path, mode):
    sp.fake.mode = mode
    listing = cf.resolve(link_from_url(SP_SHARE), tmp_path, _settings())
    assert listing.status == "needs_sign_in" and listing.files == []
    assert "sign-in" in listing.error
    assert "login.microsoftonline.com" not in sp.hosts
    assert not any("authenticate.aspx" in r.url.path or "accessdenied.aspx" in r.url.path for r in sp.requests)


def test_sharepoint_listing_403_is_a_sign_in_wall(sp, tmp_path, monkeypatch):
    monkeypatch.setattr(sp.fake, "_authed", lambda request: False)
    listing = cf.resolve(link_from_url(SP_SHARE), tmp_path, _settings())
    assert listing.status == "needs_sign_in"


def test_sharepoint_file_share_lists_one_file_from_its_metadata(sp, tmp_path):
    sp.fake.mode = "file"
    link = link_from_url(f"https://{SP_HOST}/:b:/s/EOCExternal/EbCdTok?e=1", "ignored.pdf")
    listing = cf.resolve(link, tmp_path, _settings())
    assert listing.status == "listed"
    assert [(f.path, f.size) for f in listing.files] == [("Exhibit A - Scope of Work.pdf", 401478)]
    assert listing.files[0].locator.endswith("/Exhibit%20A%20-%20Scope%20of%20Work.pdf')/$value")
    assert unquote(sp.requests[-1].url.path).endswith("Scope of Work.pdf')")
    assert sp.requests[-1].url.params["$select"] == "Name,Length"


def test_sharepoint_file_share_without_a_path_uses_download_1(sp, tmp_path):
    sp.fake.mode = "noid"
    link = link_from_url(f"https://{SP_HOST}/:b:/s/EOCExternal/EbCdTok?e=1")
    listing = cf.resolve(link, tmp_path, _settings())
    assert listing.status == "listed"
    assert [(f.path, f.size) for f in listing.files] == [("Exhibit B.pdf", len(PDF))]
    assert listing.files[0].locator == f"https://{SP_HOST}/:b:/s/EOCExternal/EbCdTok?download=1"
    assert sp.requests[-1].url.params["download"] == "1" and "FedAuth" in sp.requests[-1].headers["cookie"]
    # And download finds the jar by the share path.
    dest = tmp_path / "b.pdf"
    assert cf.download(listing.files[0].locator, dest, max_bytes=len(PDF), timeout=5) == len(PDF)
    assert "FedAuth" in sp.requests[-1].headers["cookie"]


def test_sharepoint_folder_share_landing_without_a_path_is_a_page(sp, tmp_path):
    sp.fake.mode = "noid"
    listing = cf.resolve(link_from_url(SP_SHARE), tmp_path, _settings())
    assert listing.status == "html_page" and listing.files == []


def test_sharepoint_api_page_answer_is_html_page(sp, tmp_path):
    sp.fake.mode = "api-html"
    listing = cf.resolve(link_from_url(SP_SHARE), tmp_path, _settings())
    assert listing.status == "html_page"


@pytest.mark.parametrize("mode,fragment", [
    ("503", "503"), ("transport", "ReadTimeout"), ("loop", "too many"), ("offsite", "outside"),
])
def test_sharepoint_unreachable_shapes_never_raise(sp, tmp_path, mode, fragment):
    sp.fake.mode = mode
    listing = cf.resolve(link_from_url(SP_SHARE), tmp_path, _settings())
    assert listing.status == "unreachable" and fragment in listing.error
    assert "169.254.169.254" not in sp.hosts
    assert len(sp.requests) <= cf._MAX_REDIRECTS + 1


def test_sharepoint_jar_registry_scopes_by_folder():
    a, b = httpx.Cookies(), httpx.Cookies()
    cf._register_jar("t.sharepoint.com", "/sites/S/Shared Documents/A", a)
    cf._register_jar("t.sharepoint.com", "/sites/S/Shared Documents/B", b)
    assert cf._jar_for("t.sharepoint.com", "/sites/S/Shared Documents/A/x.pdf") is a
    assert cf._jar_for("t.sharepoint.com", "/sites/S/Shared Documents/B/y/z.pdf") is b
    assert cf._jar_for("t.sharepoint.com", "/sites/S/Shared Documents/C/z.pdf") is b   # most recent
    assert cf._jar_for("other.sharepoint.com", "/x") is None
    cf.forget_jars()
    assert cf._jar_for("t.sharepoint.com", "/sites/S/Shared Documents/A/x.pdf") is None


# ── Consumer OneDrive (1drv.ms) ──────────────────────────────────────────


def test_onedrive_consumer_folder_lists_through_the_shares_api(monkeypatch, tmp_path):
    def route(request):
        assert request.url.host == "api.onedrive.com" and "cookie" not in request.headers
        path = unquote(request.url.path)
        if path.endswith("/root"):
            return _json({"id": "r", "name": "Bid Docs", "folder": {"childCount": 2}})
        if path.endswith("/root/children"):
            return _json({"value": [
                {"name": "Spec.pdf", "size": 10, "file": {}, "@content.downloadUrl": "https://x.dm.files.1drv.com/y4m/spec"},
                {"name": "Drawings", "folder": {"childCount": 1}},
            ]})
        if path.endswith("/root:/Drawings:/children"):
            return _json({"value": [{"name": "E-1.pdf", "size": 20, "file": {}}]})
        raise AssertionError(path)

    _mock(monkeypatch, route)
    listing = cf.resolve(link_from_url("https://1drv.ms/f/s!AbC"), tmp_path, _settings())
    assert listing.status == "listed"
    assert [(f.path, f.size, f.locator) for f in listing.files] == [
        ("Spec.pdf", 10, "https://x.dm.files.1drv.com/y4m/spec"),
        ("Drawings/E-1.pdf", 20, "https://api.onedrive.com/v1.0/shares/u!aHR0cHM6Ly8xZHJ2Lm1zL2YvcyFBYkM/root:/Drawings/E-1.pdf:/content"),
    ]


def test_onedrive_consumer_file_and_sign_in(monkeypatch, tmp_path):
    rec = _mock(monkeypatch, lambda r: _json({"id": "f", "name": "Quote.pdf", "size": 5, "file": {}}))
    listing = cf.resolve(link_from_url("https://1drv.ms/b/s!AbC"), tmp_path, _settings())
    assert listing.status == "listed" and [(f.path, f.size) for f in listing.files] == [("Quote.pdf", 5)]
    assert listing.files[0].locator.endswith("/root/content")
    rec.route = lambda r: httpx.Response(403)
    assert cf.resolve(link_from_url("https://1drv.ms/b/s!AbC"), tmp_path, _settings()).status == "needs_sign_in"


# ── Dropbox ──────────────────────────────────────────────────────────────


def _zip_bytes(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries:
            zf.writestr(name, data)
    return buf.getvalue()


DROPBOX_ZIP = _zip_bytes([
    ("26-080/Specs/Div 26.pdf", PDF),
    ("26-080/Drawings/E-101.pdf", PDF * 2),
    ("__MACOSX/26-080/._E-101.pdf", b"junk"),
    ("26-080/Old.zip", _zip_bytes([("x.pdf", PDF)])),
])
DROPBOX_CDN = "https://uce5c081c71967de9ed60799bfee.dl.dropboxusercontent.com/zip_download_get/CrqAF7PC#"


class FakeDropbox:
    def __init__(self):
        self.mode = "ok"
        self.body = DROPBOX_ZIP

    def __call__(self, request):
        assert "cookie" not in request.headers, "cookies must never reach Dropbox"
        if request.url.host == "www.dropbox.com":
            assert request.url.params["dl"] == "1"
            if self.mode == "password":
                return _html('<form><input type="password" name="password"></form>')
            if self.mode == "page":
                return _html("<html>Shared folder</html>")
            if self.mode == "offsite":
                return _redirect("https://evil.example/zip")
            if request.url.path.startswith("/scl/fi/") or request.url.path.startswith("/s/"):
                return _redirect("https://uc1.dl.dropboxusercontent.com/cd/0/get/TOKEN/file")
            return _redirect(DROPBOX_CDN)
        if request.url.host.endswith(".dl.dropboxusercontent.com"):
            if request.url.path.startswith("/cd/"):
                return _pdf("Spec Book.pdf", body=PDF, **{"content-length": str(len(PDF))})
            return httpx.Response(200, content=self.body, headers={"content-type": "application/zip"})
        raise AssertionError(request.url)


@pytest.fixture
def dropbox(monkeypatch):
    fake = FakeDropbox()
    rec = _mock(monkeypatch, fake)
    rec.fake = fake
    return rec


def test_dropbox_folder_streams_the_zip_and_lists_its_members(dropbox, tmp_path):
    link = link_from_url(DROPBOX_FOLDER, "Bid Documents")
    listing = cf.resolve(link, tmp_path, _settings())
    assert listing.status == "listed" and not listing.truncated
    assert listing.zip_path and listing.zip_path.startswith(str(tmp_path))
    assert listing.zip_path.endswith(".zip")
    assert [(f.path, f.size) for f in listing.files] == [
        ("26-080/Specs/Div 26.pdf", len(PDF)),
        ("26-080/Drawings/E-101.pdf", len(PDF) * 2),
    ]
    assert listing.files[1].locator == f"zip:{listing.zip_path}|1"
    assert dropbox.hosts == ["www.dropbox.com", "uce5c081c71967de9ed60799bfee.dl.dropboxusercontent.com"]
    # The member inflates through the zip locator; the nested zip is not listed.
    dest = tmp_path / "e101.pdf"
    assert cf.download(listing.files[1].locator, dest, max_bytes=len(PDF) * 2, timeout=5) == len(PDF) * 2
    assert dest.read_bytes() == PDF * 2
    with pytest.raises(CloudForbidden) as exc:
        cf.download(listing.files[1].locator, tmp_path / "small.pdf", max_bytes=100, timeout=5)
    assert "larger than the per-file limit" in str(exc.value)
    assert not (tmp_path / "small.pdf").exists()


def test_dropbox_folder_password_page_and_plain_page(dropbox, tmp_path):
    dropbox.fake.mode = "password"
    listing = cf.resolve(link_from_url(DROPBOX_FOLDER), tmp_path, _settings())
    assert listing.status == "needs_sign_in"
    dropbox.fake.mode = "page"
    listing = cf.resolve(link_from_url(DROPBOX_FOLDER), tmp_path, _settings())
    assert listing.status == "html_page"
    assert not list(tmp_path.glob("*.zip"))


def test_dropbox_folder_over_the_total_cap(dropbox, tmp_path):
    listing = cf.resolve(link_from_url(DROPBOX_FOLDER), tmp_path, _settings(rfp_harvest_max_total_bytes=500))
    assert listing.status == "too_many_files" and "larger than the harvest accepts" in listing.error
    assert not list(tmp_path.glob("*.zip"))


def test_dropbox_folder_unreadable_zip(dropbox, tmp_path):
    dropbox.fake.body = b"PK\x03\x04 truncated junk"
    listing = cf.resolve(link_from_url(DROPBOX_FOLDER), tmp_path, _settings())
    assert listing.status == "unreachable" and listing.error and listing.zip_path


def test_dropbox_folder_redirect_offsite_is_refused(dropbox, tmp_path):
    dropbox.fake.mode = "offsite"
    listing = cf.resolve(link_from_url(DROPBOX_FOLDER), tmp_path, _settings())
    assert listing.status == "unreachable" and "evil.example" not in dropbox.hosts


def test_dropbox_file_lists_one_file_from_the_headers(dropbox, tmp_path):
    link = link_from_url("https://www.dropbox.com/scl/fi/abc/Spec%20Book.pdf?rlkey=k&dl=0")
    listing = cf.resolve(link, tmp_path, _settings())
    assert listing.status == "listed" and listing.zip_path is None
    assert [(f.path, f.size) for f in listing.files] == [("Spec Book.pdf", len(PDF))]
    assert listing.files[0].locator == "https://www.dropbox.com/scl/fi/abc/Spec%20Book.pdf?rlkey=k&dl=1"
    dest = tmp_path / "s.pdf"
    assert cf.download(listing.files[0].locator, dest, max_bytes=len(PDF), timeout=5) == len(PDF)


def test_dropbox_never_sees_a_sharepoint_jar(dropbox, tmp_path):
    jar = httpx.Cookies()
    jar.set("FedAuth", "guest-token", domain=SP_HOST)
    cf._register_jar(SP_HOST, "/", jar)
    cf.resolve(link_from_url(DROPBOX_FOLDER), tmp_path, _settings())
    assert all(c is None for c in dropbox.cookies_sent_to("www.dropbox.com"))


# ── Google Drive ─────────────────────────────────────────────────────────


CONFIRM_PAGE = """<!DOCTYPE html><html><body>
<form id="download-form" action="https://drive.usercontent.google.com/download" method="get">
<input type="hidden" name="id" value="1BiG"><input type="hidden" name="export" value="download">
<input type="hidden" name="confirm" value="t"><input type="hidden" name="uuid" value="u-u-i-d">
</form><span class="uc-name-size"><a href="/open?id=1BiG">Bid Set.pdf</a> (120M)</span>
</body></html>"""


class FakeDrive:
    def __init__(self):
        self.mode = "ok"
        self.files = {
            "1SmAll": ("Small.pdf", PDF),
            "1BiG": ("Bid Set.pdf", PDF * 5),
        }

    def __call__(self, request):
        assert "cookie" not in request.headers
        host, path = request.url.host, request.url.path
        if host == "drive.google.com" and path == "/uc":
            file_id = request.url.params["id"]
            if self.mode == "signin":
                return _redirect("https://accounts.google.com/ServiceLogin?continue=x")
            if self.mode == "404":
                return httpx.Response(404)
            name, body = self.files[file_id]
            if file_id == "1BiG":
                return _html(CONFIRM_PAGE)
            return _pdf(name, body=body, **{"content-length": str(len(body))})
        if host == "drive.usercontent.google.com" and path == "/download":
            p = request.url.params
            assert p["confirm"] == "t" and p["uuid"] == "u-u-i-d" and p["export"] == "download"
            name, body = self.files[p["id"]]
            return _pdf(name, body=body)
        if host == "www.googleapis.com" and path == "/drive/v3/files":
            p = request.url.params
            assert p["key"] == "test-key" and p["fields"] == "nextPageToken,files(id,name,size,mimeType)"
            assert p["pageSize"] == "200"
            if self.mode == "403":
                return httpx.Response(403, content=b'{"error":{"code":403}}')
            parent = p["q"].split("'")[1]
            if parent == "1FoLdEr":
                if p.get("pageToken") == "p2":
                    return _json({"files": [{"id": "1Sub", "name": "Addendum 1", "mimeType": cf._GDRIVE_FOLDER_MIME}]})
                return _json({"nextPageToken": "p2", "files": [
                    {"id": "1SmAll", "name": "Small.pdf", "size": "3012", "mimeType": "application/pdf"},
                    {"id": "1DoC", "name": "Bid Form", "mimeType": "application/vnd.google-apps.document"},
                ]})
            if parent == "1Sub":
                return _json({"files": [{"id": "1BiG", "name": "Bid Set.pdf", "size": "15060", "mimeType": "application/pdf"}]})
            if parent == "1OnlyDocs":
                return _json({"files": [{"id": "1DoC", "name": "Bid Form", "mimeType": "application/vnd.google-apps.spreadsheet"}]})
            raise AssertionError(parent)
        if host == "drive.google.com" and path == "/embeddedfolderview":
            folder_id = request.url.params["id"]
            if self.mode == "signin":
                return _redirect("https://accounts.google.com/ServiceLogin?continue=x")
            if folder_id == "1FoLdEr":
                return _html(EMBED_ROOT)
            if folder_id == "1Sub":
                return _html(EMBED_SUB)
            raise AssertionError(folder_id)
        raise AssertionError(request.url)


EMBED_ROOT = """<html><body><div class="flip-entries">
<div class="flip-entry" id="entry-1SmAll"><a href="https://drive.google.com/file/d/1SmAll/view?usp=drive_web" target="_blank">
<div class="flip-entry-thumb"><div class="flip-entry-icon" style="background-image:url(https://drive-thirdparty.googleusercontent.com/64/type/application/pdf)"></div></div>
<div class="flip-entry-info"><div class="flip-entry-title">Small &amp; Co.pdf</div><div class="flip-entry-last-modified"><div>Sep 1, 2026</div></div></div></a></div>
<div class="flip-entry" id="entry-1Sub"><a href="https://drive.google.com/embeddedfolderview?id=1Sub#list">
<div class="flip-entry-thumb"><div class="flip-entry-icon" style="background-image:url(https://drive-thirdparty.googleusercontent.com/64/type/application/vnd.google-apps.folder)"></div></div>
<div class="flip-entry-info"><div class="flip-entry-title">Addendum 1</div></div></a></div>
<div class="flip-entry" id="entry-1DoC"><a href="https://docs.google.com/document/d/1DoC/edit">
<div class="flip-entry-thumb"><div class="flip-entry-icon" style="background-image:url(https://drive-thirdparty.googleusercontent.com/64/type/application/vnd.google-apps.document)"></div></div>
<div class="flip-entry-info"><div class="flip-entry-title">Bid Form</div></div></a></div>
</div></body></html>"""
EMBED_SUB = """<html><body>
<div class="flip-entry" id="entry-1BiG"><a href="https://drive.google.com/file/d/1BiG/view">
<div class="flip-entry-thumb"><div class="flip-entry-icon" style="background-image:url(https://drive-thirdparty.googleusercontent.com/64/type/application/pdf)"></div></div>
<div class="flip-entry-info"><div class="flip-entry-title">Bid Set.pdf</div></div></a></div>
</body></html>"""


@pytest.fixture
def drive(monkeypatch):
    fake = FakeDrive()
    rec = _mock(monkeypatch, fake)
    rec.fake = fake
    return rec


def test_gdrive_small_file_lists_and_downloads(drive, tmp_path):
    link = link_from_url("https://drive.google.com/file/d/1SmAll/view?usp=sharing")
    listing = cf.resolve(link, tmp_path, _settings())
    assert listing.status == "listed"
    assert [(f.path, f.size, f.locator) for f in listing.files] == [
        ("Small.pdf", len(PDF), "https://drive.google.com/uc?export=download&id=1SmAll"),
    ]
    dest = tmp_path / "small.pdf"
    assert cf.download(listing.files[0].locator, dest, max_bytes=len(PDF), timeout=5) == len(PDF)
    assert dest.read_bytes() == PDF


def test_gdrive_large_file_confirm_page_is_followed_once(drive, tmp_path):
    link = link_from_url("https://drive.google.com/open?id=1BiG", "from-label.pdf")
    listing = cf.resolve(link, tmp_path, _settings())
    assert listing.status == "listed"
    assert [(f.path, f.size) for f in listing.files] == [("Bid Set.pdf", None)]
    dest = tmp_path / "big.pdf"
    n = len(drive.requests)
    assert cf.download(listing.files[0].locator, dest, max_bytes=len(PDF) * 5, timeout=5) == len(PDF) * 5
    assert dest.read_bytes() == PDF * 5
    assert [r.url.host for r in drive.requests[n:]] == ["drive.google.com", "drive.usercontent.google.com"]
    assert str(drive.requests[-1].url).startswith(
        "https://drive.usercontent.google.com/download?id=1BiG&export=download&confirm=t&uuid=u-u-i-d"
    )


def test_gdrive_confirm_page_that_keeps_answering_a_page_is_forbidden(drive, tmp_path, monkeypatch):
    calls = []

    def route(request):
        calls.append(request.url.host)
        return _html(CONFIRM_PAGE)

    drive.route = route
    with pytest.raises(CloudForbidden) as exc:
        cf.download("https://drive.google.com/uc?export=download&id=1BiG", tmp_path / "x.pdf", max_bytes=10_000, timeout=5)
    assert "web page" in str(exc.value) and calls == ["drive.google.com", "drive.usercontent.google.com"]
    assert not (tmp_path / "x.pdf").exists()


@pytest.mark.parametrize("mode", ["signin", "404"])
def test_gdrive_file_sign_in(drive, tmp_path, mode):
    drive.fake.mode = mode
    listing = cf.resolve(link_from_url("https://drive.google.com/file/d/1SmAll/view"), tmp_path, _settings())
    assert listing.status == "needs_sign_in" and "accounts.google.com" not in drive.hosts


def test_gdrive_folder_via_api_key_pages_recurses_and_skips_native_docs(drive, tmp_path):
    link = link_from_url("https://drive.google.com/drive/folders/1FoLdEr?usp=sharing")
    listing = cf.resolve(link, tmp_path, _settings(google_drive_api_key="test-key"))
    assert listing.status == "listed" and listing.error is None and not listing.truncated
    assert [(f.path, f.size, f.locator) for f in listing.files] == [
        ("Small.pdf", 3012, "https://drive.google.com/uc?export=download&id=1SmAll"),
        ("Addendum 1/Bid Set.pdf", 15060, "https://drive.google.com/uc?export=download&id=1BiG"),
    ]
    qs = [r.url.params["q"] for r in drive.requests]
    assert qs == ["'1FoLdEr' in parents", "'1FoLdEr' in parents", "'1Sub' in parents"]
    assert drive.requests[1].url.params["pageToken"] == "p2"
    assert all(r.url.host == "www.googleapis.com" for r in drive.requests)


def test_gdrive_folder_via_api_only_native_docs_says_so(drive, tmp_path):
    link = link_from_url("https://drive.google.com/drive/folders/1OnlyDocs")
    listing = cf.resolve(link, tmp_path, _settings(google_drive_api_key="test-key"))
    assert listing.status == "listed" and listing.files == []
    assert "Google Docs" in listing.error


def test_gdrive_folder_via_api_403_is_sign_in(drive, tmp_path):
    drive.fake.mode = "403"
    link = link_from_url("https://drive.google.com/drive/folders/1FoLdEr")
    assert cf.resolve(link, tmp_path, _settings(google_drive_api_key="test-key")).status == "needs_sign_in"


def test_gdrive_folder_via_embedded_view_without_a_key(drive, tmp_path):
    link = link_from_url("https://drive.google.com/drive/folders/1FoLdEr")
    listing = cf.resolve(link, tmp_path, _settings(google_drive_api_key=""))
    assert listing.status == "listed" and listing.error is None
    assert [(f.path, f.size, f.locator) for f in listing.files] == [
        ("Small & Co.pdf", None, "https://drive.google.com/uc?export=download&id=1SmAll"),
        ("Addendum 1/Bid Set.pdf", None, "https://drive.google.com/uc?export=download&id=1BiG"),
    ]
    assert [(r.url.path, r.url.params["id"]) for r in drive.requests] == [
        ("/embeddedfolderview", "1FoLdEr"), ("/embeddedfolderview", "1Sub"),
    ]


def test_gdrive_folder_via_embedded_view_sign_in(drive, tmp_path):
    drive.fake.mode = "signin"
    link = link_from_url("https://drive.google.com/drive/folders/1FoLdEr")
    listing = cf.resolve(link, tmp_path, _settings())
    assert listing.status == "needs_sign_in" and "accounts.google.com" not in drive.hosts


def test_gdrive_depth_cap_applies_to_folders(drive, tmp_path):
    link = link_from_url("https://drive.google.com/drive/folders/1FoLdEr")
    listing = cf.resolve(link, tmp_path, _settings(google_drive_api_key="test-key", rfp_harvest_folder_max_depth=0))
    assert listing.status == "listed" and listing.truncated
    assert [f.path for f in listing.files] == ["Small.pdf"]


def test_google_docs_links_are_unsupported_without_a_request(monkeypatch, tmp_path):
    _mock(monkeypatch, _refuse)
    link = link_from_url("https://docs.google.com/spreadsheets/d/1ShEeT/edit")
    listing = cf.resolve(link, tmp_path, _settings())
    assert listing == Listing("unsupported", [], "Google Docs links must be downloaded by hand.", False)


# ── Box ──────────────────────────────────────────────────────────────────


def test_box_file_lists_one_file_from_the_redirect(monkeypatch, tmp_path):
    def route(request):
        assert "cookie" not in request.headers
        if request.url.host == "app.box.com":
            assert request.url.path == "/index.php"
            assert request.url.params["rm"] == "box_download_shared_file"
            assert request.url.params["shared_name"] == "abc123XYZ"
            return _redirect("https://public.boxcloud.com/d/1/b1!abc/download")
        assert request.url.host == "public.boxcloud.com"
        return _pdf("Box Quote.pdf", body=PDF * 100, **{"content-length": str(len(PDF) * 100)})

    rec = _mock(monkeypatch, route)
    listing = cf.resolve(link_from_url("https://gc.app.box.com/s/abc123XYZ"), tmp_path, _settings())
    assert listing.status == "listed"
    assert [(f.path, f.size) for f in listing.files] == [("Box Quote.pdf", len(PDF) * 100)]
    assert listing.files[0].locator == "https://app.box.com/index.php?rm=box_download_shared_file&shared_name=abc123XYZ"
    assert rec.hosts == ["app.box.com", "public.boxcloud.com"]
    dest = tmp_path / "box.pdf"
    assert cf.download(listing.files[0].locator, dest, max_bytes=len(PDF) * 100, timeout=5) == len(PDF) * 100


def test_box_folder_is_unsupported(monkeypatch, tmp_path):
    _mock(monkeypatch, lambda r: _html("<html>Box folder</html>"))
    listing = cf.resolve(link_from_url("https://app.box.com/s/abc123XYZ"), tmp_path, _settings())
    assert listing == Listing("unsupported", [], "Box folders must be downloaded by hand.", False)


def test_box_password_wall(monkeypatch, tmp_path):
    _mock(monkeypatch, lambda r: httpx.Response(403))
    assert cf.resolve(link_from_url("https://app.box.com/s/abc123XYZ"), tmp_path, _settings()).status == "needs_sign_in"


# ── Hands-off providers ──────────────────────────────────────────────────


@pytest.mark.parametrize("url,name", [
    ("https://sletteninc.sharefile.com/d-s5272283033994a1b9120d20ad63b4fff", "ShareFile"),
    ("https://gc.egnyte.com/fl/abc123", "Egnyte"),
    ("https://we.tl/t-AbC123", "WeTransfer"),
    ("https://spaces.hightail.com/space/AbC", "Hightail"),
])
def test_unsupported_providers_make_no_request(monkeypatch, tmp_path, url, name):
    _mock(monkeypatch, _refuse)
    link = link_from_url(url)
    assert not link.supported
    listing = cf.resolve(link, tmp_path, _settings())
    assert listing == Listing("unsupported", [], f"{name} links must be downloaded by hand.", False)


def test_resolve_never_raises_on_transport_trouble(monkeypatch, tmp_path):
    def route(request):
        raise httpx.ConnectError("dns", request=request)

    _mock(monkeypatch, route)
    for url in (SP_SHARE, DROPBOX_FOLDER, "https://drive.google.com/file/d/1X/view", "https://app.box.com/s/abc"):
        listing = cf.resolve(link_from_url(url), tmp_path, _settings())
        assert listing.status == "unreachable" and "ConnectError" in listing.error


# ── download: SSRF guards, caps, error mapping ───────────────────────────


@pytest.mark.parametrize("locator", [
    "http://www.dropbox.com/s/abc/q.pdf?dl=1",
    "https://storage.procore.com/x",
    "https://169.254.169.254/latest/meta-data/",
    "https://www.dropbox.com.evil.example/x",
    "ftp://www.dropbox.com/x",
    "zip:/nowhere/a.zip|x",
    "zip:|1",
])
def test_download_refuses_bad_locators_before_any_request(monkeypatch, tmp_path, locator):
    rec = _mock(monkeypatch, _refuse)
    dest = tmp_path / "x.bin"
    with pytest.raises(CloudForbidden):
        cf.download(locator, dest, max_bytes=1000, timeout=5)
    assert rec.requests == [] and not dest.exists()


@pytest.mark.parametrize("location", [
    "https://169.254.169.254/latest/meta-data/",
    "http://www.dropbox.com/plain",
    "https://internal.corp.example/file.pdf",
])
def test_download_refuses_redirects_off_the_allowlist(monkeypatch, tmp_path, location):
    rec = _mock(monkeypatch, lambda r: _redirect(location))
    dest = tmp_path / "x.bin"
    with pytest.raises(CloudForbidden) as exc:
        cf.download("https://www.dropbox.com/s/abc/q.pdf?dl=1", dest, max_bytes=1000, timeout=5)
    assert "outside" in str(exc.value)
    assert rec.hosts == ["www.dropbox.com"] and not dest.exists()


def test_download_caps_on_received_bytes_with_a_lying_content_length(monkeypatch, tmp_path):
    _mock(monkeypatch, lambda r: httpx.Response(200, content=b"x" * 5000, headers={
        "content-type": "application/pdf", "content-length": "10",
    }))
    dest = tmp_path / "x.bin"
    with pytest.raises(CloudForbidden) as exc:
        cf.download("https://www.dropbox.com/s/abc/q.pdf?dl=1", dest, max_bytes=1024, timeout=5)
    assert "larger than the per-file limit" in str(exc.value)
    assert not dest.exists()


def test_download_exact_cap_is_allowed(monkeypatch, tmp_path):
    _mock(monkeypatch, lambda r: httpx.Response(200, content=b"x" * 1024, headers={"content-type": "application/pdf"}))
    dest = tmp_path / "x.bin"
    assert cf.download("https://www.dropbox.com/s/abc/q.pdf?dl=1", dest, max_bytes=1024, timeout=5) == 1024


def test_download_follows_up_to_five_hops_and_refuses_a_sixth(monkeypatch, tmp_path):
    def route(request):
        n = int(request.url.params.get("n", "0"))
        if n < 6:
            return _redirect(f"https://www.dropbox.com/hop?n={n + 1}")
        return httpx.Response(200, content=PDF, headers={"content-type": "application/pdf"})

    rec = _mock(monkeypatch, route)
    dest = tmp_path / "x.bin"
    with pytest.raises(CloudForbidden) as exc:
        cf.download("https://www.dropbox.com/hop?n=0", dest, max_bytes=10_000, timeout=5)
    assert "too many times" in str(exc.value) and len(rec.requests) == cf._MAX_REDIRECTS + 1
    rec.route = lambda r: (
        _redirect(f"https://www.dropbox.com/hop?n={int(r.url.params.get('n', '0')) + 1}")
        if int(r.url.params.get("n", "0")) < 5
        else httpx.Response(200, content=PDF, headers={"content-type": "application/pdf"})
    )
    assert cf.download("https://www.dropbox.com/hop?n=0", dest, max_bytes=10_000, timeout=5) == len(PDF)


@pytest.mark.parametrize("status,error", [
    (401, CloudForbidden), (403, CloudForbidden), (404, CloudForbidden), (410, CloudForbidden),
    (429, CloudTransient), (500, CloudTransient), (503, CloudTransient),
])
def test_download_maps_status_codes(monkeypatch, tmp_path, status, error):
    _mock(monkeypatch, lambda r: httpx.Response(status))
    dest = tmp_path / "x.bin"
    with pytest.raises(error):
        cf.download("https://www.dropbox.com/s/abc/q.pdf?dl=1", dest, max_bytes=1000, timeout=5)
    assert not dest.exists()


def test_download_maps_transport_trouble_to_transient(monkeypatch, tmp_path):
    def route(request):
        raise httpx.ReadTimeout("slow", request=request)

    _mock(monkeypatch, route)
    dest = tmp_path / "x.bin"
    with pytest.raises(CloudTransient) as exc:
        cf.download("https://www.dropbox.com/s/abc/q.pdf?dl=1", dest, max_bytes=1000, timeout=5)
    assert "ReadTimeout" in str(exc.value) and not dest.exists()


def test_download_refuses_a_page_where_bytes_were_expected(monkeypatch, tmp_path):
    rec = _mock(monkeypatch, lambda r: _html("<html><body>Sign in</body></html>"))
    dest = tmp_path / "x.bin"
    with pytest.raises(CloudForbidden) as exc:
        cf.download("https://www.dropbox.com/s/abc/q.pdf?dl=1", dest, max_bytes=1000, timeout=5)
    assert "web page" in str(exc.value) and not dest.exists()
    # A page served as octet-stream is sniffed, not trusted.
    rec.route = lambda r: httpx.Response(200, content=b"\n<!DOCTYPE html><html>x</html>",
                                         headers={"content-type": "application/octet-stream"})
    with pytest.raises(CloudForbidden):
        cf.download("https://www.dropbox.com/s/abc/q.pdf?dl=1", dest, max_bytes=1000, timeout=5)
    assert not dest.exists()


def test_download_never_overwrites(monkeypatch, tmp_path):
    rec = _mock(monkeypatch, _refuse)
    dest = tmp_path / "x.bin"
    dest.write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        cf.download("https://www.dropbox.com/s/abc/q.pdf?dl=1", dest, max_bytes=1000, timeout=5)
    assert rec.requests == [] and dest.read_bytes() == b"keep"


def test_download_validates_the_cap(tmp_path):
    with pytest.raises(ValueError):
        cf.download("https://www.dropbox.com/s/abc/q.pdf?dl=1", tmp_path / "x", max_bytes=0)


def test_download_zip_locator_errors_map_to_forbidden(tmp_path):
    zip_path = tmp_path / "a.zip"
    zip_path.write_bytes(_zip_bytes([("a.pdf", PDF)]))
    dest = tmp_path / "a.pdf"
    assert cf.download(f"zip:{zip_path}|0", dest, max_bytes=len(PDF)) == len(PDF)
    with pytest.raises(CloudForbidden):
        cf.download(f"zip:{zip_path}|9", tmp_path / "b.pdf", max_bytes=len(PDF))
    with pytest.raises(CloudForbidden) as exc:
        cf.download(f"zip:{zip_path}|0", tmp_path / "c.pdf", max_bytes=10)
    assert "larger than the per-file limit" in str(exc.value)
    assert not (tmp_path / "b.pdf").exists() and not (tmp_path / "c.pdf").exists()


def test_download_sends_the_jar_only_to_its_sharepoint_host(monkeypatch, tmp_path):
    jar = httpx.Cookies()
    jar.set("FedAuth", "guest-token", domain=SP_HOST)
    cf._register_jar(SP_HOST, "/sites/EOCExternal", jar)
    seen = {}

    def route(request):
        seen[request.url.host] = request.headers.get("cookie")
        if request.url.host == SP_HOST:
            return _redirect("https://api.onedrive.com/v1.0/x/content")
        return httpx.Response(200, content=PDF, headers={"content-type": "application/pdf"})

    _mock(monkeypatch, route)
    locator = f"https://{SP_HOST}/sites/EOCExternal/_api/web/GetFileByServerRelativeUrl('/sites/EOCExternal/x.pdf')/$value"
    assert cf.download(locator, tmp_path / "x.pdf", max_bytes=len(PDF), timeout=5) == len(PDF)
    assert seen[SP_HOST] == "FedAuth=guest-token" and seen["api.onedrive.com"] is None
    # A different tenant never sees it either.
    seen.clear()
    assert cf.download("https://other.sharepoint.com/:b:/s/X/T?download=1", tmp_path / "y.pdf", max_bytes=len(PDF), timeout=5) == len(PDF)
    assert seen["other.sharepoint.com"] is None


def test_download_jar_never_crosses_tenants_even_on_a_sharepoint_hop(monkeypatch, tmp_path):
    # Both hosts are *.sharepoint.com (the jar code path is active on each),
    # but the cookie belongs to the first tenant only.
    jar = httpx.Cookies()
    seen = {}

    def route(request):
        seen[request.url.host] = request.headers.get("cookie")
        if request.url.host == SP_HOST and request.url.path.startswith("/:b:"):
            return _redirect("https://other-tenant.sharepoint.com/sites/X/_layouts/15/download.aspx?x=1",
                             **{"set-cookie": "FedAuth=first-tenant; path=/; secure; HttpOnly"})
        return httpx.Response(200, content=PDF, headers={"content-type": "application/pdf"})

    _mock(monkeypatch, route)
    # Redeem into the jar through the real hop code, then park it.
    with cf._client(5) as client:
        with cf._open(client, f"https://{SP_HOST}/:b:/s/X/T", jar=jar):
            pass
    cf._register_jar(SP_HOST, "/:b:/s/X/T", jar)
    seen.clear()
    assert cf.download(f"https://{SP_HOST}/:b:/s/X/T?download=1", tmp_path / "x.pdf", max_bytes=len(PDF), timeout=5) == len(PDF)
    assert seen[SP_HOST] == "FedAuth=first-tenant"
    assert seen["other-tenant.sharepoint.com"] is None


def test_download_reads_the_timeout_from_settings_when_not_given(monkeypatch, tmp_path):
    captured = {}

    def client(timeout):
        captured["timeout"] = timeout
        return httpx.Client(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, content=PDF, headers={"content-type": "application/pdf"})
        ), follow_redirects=False)

    monkeypatch.setattr(cf, "_client", client)
    cf.download("https://www.dropbox.com/s/abc/q.pdf?dl=1", tmp_path / "x.pdf", max_bytes=len(PDF))
    assert captured["timeout"] == Settings(_env_file=None).cloud_request_timeout_seconds


# ── Contract shapes ──────────────────────────────────────────────────────


def test_public_shapes_match_the_contract():
    link = ShareLink("https://x", "", "box", "box:x", "unknown", True)
    assert (link.url, link.label, link.provider, link.key, link.kind, link.supported) == (
        "https://x", "", "box", "box:x", "unknown", True
    )
    rf = RemoteFile("a/b.pdf", None, "https://x")
    assert (rf.path, rf.size, rf.locator) == ("a/b.pdf", None, "https://x")
    listing = Listing("unsupported", [], "x", False)
    assert listing.zip_path is None
    assert issubclass(CloudTransient, cf.CloudError) and issubclass(CloudForbidden, cf.CloudError)
    assert cf.CloudUnavailable("x").locked_until is None
    assert issubclass(cf.CloudUnavailable, cf.CloudError)
    assert parse_qs(urlparse(cf._gdrive_file_url("a b")).query) == {"export": ["download"], "id": ["a b"]}
    assert quote("x") == "x"
