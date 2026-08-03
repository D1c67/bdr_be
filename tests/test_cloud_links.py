"""cloud_links: share-link detection in reply bodies, direct-download
resolution per provider, and the SSRF-guarded fetcher."""

import httpx
import pytest

from app.services import cloud_links
from app.services.cloud_links import CloudLink, CloudLinkError

# A trimmed real Outlook "sharing link" reply body: safelinks-wrapped href with
# the true target preserved in originalsrc, filename as the anchor text.
OUTLOOK_BODY = """
<html><body><div><span class="_Entity _EType_om-sharing-link-entity">
<a rel="noreferrer noopener"
   href="https://nam09.safelinks.protection.outlook.com/?url=https%3A%2F%2Fvendor-my.sharepoint.com%2F%3Ab%3A%2Fp%2Fjane%2FIQAL2Dl%3Fxsdata%3DMDV8MDJ8&amp;sdata=eGhj"
   originalsrc="https://vendor-my.sharepoint.com/:b:/p/jane/IQAL2Dl"
   class="ms-outlook-mobile-sharing-link-anchor">
<img src="https://spoprod-a.akamaihd.net/pdf.svg">CODALE LIGHTING QUOTE.pdf</a></span>
<div>Link test.</div></body></html>
"""


def test_finds_outlook_sharing_link_with_filename_label():
    links = cloud_links.find_cloud_links(OUTLOOK_BODY)
    assert len(links) == 1
    link = links[0]
    assert link.provider == "onedrive"
    assert link.url == "https://vendor-my.sharepoint.com/:b:/p/jane/IQAL2Dl"
    assert link.label == "CODALE LIGHTING QUOTE.pdf"


def test_safelinks_href_unwraps_when_no_originalsrc():
    body = (
        '<a href="https://nam09.safelinks.protection.outlook.com/?url='
        "https%3A%2F%2Fwww.dropbox.com%2Fs%2Fabc123%2Fquote.pdf%3Fdl%3D0"
        '&amp;sdata=x">quote.pdf</a>'
    )
    links = cloud_links.find_cloud_links(body)
    assert len(links) == 1
    assert links[0].provider == "dropbox"
    assert links[0].url == "https://www.dropbox.com/s/abc123/quote.pdf?dl=0"


def test_bare_url_in_plain_text_is_found():
    links = cloud_links.find_cloud_links(
        "Here you go: https://drive.google.com/file/d/FILE-ID_123/view?usp=sharing"
    )
    assert len(links) == 1
    assert links[0].provider == "gdrive"


def test_non_cloud_links_and_images_ignored():
    body = (
        '<a href="https://example.com/quote.pdf">quote</a>'
        '<a href="https://aka.ms/GetOutlookForMac">Outlook</a>'
        '<img src="https://spoprod-a.akamaihd.net/pdf.svg">'
    )
    assert cloud_links.find_cloud_links(body) == []


def test_anchor_and_regex_twin_dedupe_to_one():
    # The same share appears as an anchor AND as raw text; tracking params differ.
    body = (
        '<a href="https://vendor-my.sharepoint.com/:b:/p/j/TOKEN?e=abc">q.pdf</a> '
        "https://vendor-my.sharepoint.com/:b:/p/j/TOKEN?e=xyz"
    )
    links = cloud_links.find_cloud_links(body)
    assert len(links) == 1
    assert links[0].label == "q.pdf"  # anchor (labelled) wins


def test_merge_links_dedupes_across_sources():
    a = cloud_links.link_from_url("https://1drv.ms/b/s!token", "a.pdf")
    b = cloud_links.link_from_url("https://1drv.ms/b/s!token")
    merged = cloud_links.merge_links([a], [b])
    assert len(merged) == 1


# ── Candidate resolution ─────────────────────────────────────────────────────


def test_onedrive_candidates_try_anon_only_never_graph():
    link = cloud_links.link_from_url("https://vendor-my.sharepoint.com/:b:/p/j/TOK")
    kinds = [k for k, _ in cloud_links._candidates(link)]
    urls = [u for _, u in cloud_links._candidates(link)]
    # Anonymous only. The Graph /shares app-token candidate was removed: pointing
    # the org's tenant-wide token at a vendor-supplied URL is a confused deputy.
    assert kinds == ["plain", "plain"]
    assert urls[0].endswith("?download=1")
    assert urls[1].startswith("https://api.onedrive.com/v1.0/shares/u!")


def test_no_provider_ever_yields_a_graph_candidate():
    # Regression guard for the confused-deputy SSRF: no share URL, whatever the
    # provider, may be fetched with the org's Graph application token.
    for url in (
        "https://vendor-my.sharepoint.com/:b:/p/j/TOK",
        "https://1drv.ms/b/s!tok",
        "https://drive.google.com/file/d/FILE-ID_123/view",
        "https://www.dropbox.com/s/abc/q.pdf?dl=0",
        "https://app.box.com/s/tok3n",
    ):
        link = cloud_links.link_from_url(url)
        assert all(kind == "plain" for kind, _ in cloud_links._candidates(link))
    assert not hasattr(cloud_links, "_graph_fetch")


def test_find_cloud_links_caps_link_count():
    # A hostile reply stuffed with tens of thousands of distinct allowlisted URLs
    # must not translate into unbounded downstream per-link work.
    body = "".join(
        f'<a href="https://tenant.sharepoint.com/{i}">f{i}</a>'
        for i in range(cloud_links._MAX_LINKS + 50)
    )
    assert len(cloud_links.find_cloud_links(body)) == cloud_links._MAX_LINKS


def test_gdrive_candidate_extracts_file_id():
    link = cloud_links.link_from_url("https://drive.google.com/file/d/FILE-ID_123/view")
    assert cloud_links._candidates(link) == [
        ("plain", "https://drive.google.com/uc?export=download&id=FILE-ID_123")
    ]


def test_gdrive_folder_link_has_no_candidates():
    link = cloud_links.link_from_url("https://drive.google.com/drive/folders/xyz")
    assert cloud_links._candidates(link) == []


def test_dropbox_candidate_flips_dl_param():
    link = cloud_links.link_from_url("https://www.dropbox.com/s/abc/q.pdf?dl=0")
    [(_, url)] = cloud_links._candidates(link)
    assert url == "https://www.dropbox.com/s/abc/q.pdf?dl=1"


def test_box_candidate_uses_shared_name():
    link = cloud_links.link_from_url("https://app.box.com/s/tok3n")
    [(_, url)] = cloud_links._candidates(link)
    assert "box_download_shared_file" in url and "shared_name=tok3n" in url


# ── Fetcher (mocked transport) ───────────────────────────────────────────────

PDF = b"%PDF-1.7 fake body"


def _mock_client(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        cloud_links, "_client",
        lambda: httpx.Client(transport=transport, follow_redirects=False),
    )


def test_fetch_happy_path_uses_content_disposition_name(monkeypatch):
    def handler(request):
        assert request.url.params.get("download") == "1"
        return httpx.Response(
            200, content=PDF,
            headers={"content-type": "application/pdf",
                     "content-disposition": 'attachment; filename="Real Quote.pdf"'},
        )

    _mock_client(monkeypatch, handler)
    link = CloudLink("https://vendor-my.sharepoint.com/:b:/p/j/TOK", "label.pdf", "onedrive")
    fetched = cloud_links.fetch(link, max_bytes=1024)
    assert fetched.filename == "Real Quote.pdf"
    assert fetched.content == PDF


def test_fetch_falls_back_to_label_for_filename(monkeypatch):
    _mock_client(monkeypatch, lambda r: httpx.Response(
        200, content=PDF, headers={"content-type": "application/pdf"}))
    link = CloudLink("https://vendor-my.sharepoint.com/:b:/p/j/TOK",
                     "CODALE LIGHTING QUOTE.pdf", "onedrive")
    assert cloud_links.fetch(link, max_bytes=1024).filename == "CODALE LIGHTING QUOTE.pdf"


def test_fetch_redirect_to_unlisted_host_is_refused(monkeypatch):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://169.254.169.254/latest/"})

    _mock_client(monkeypatch, handler)
    link = CloudLink("https://www.dropbox.com/s/abc/q.pdf", "q.pdf", "dropbox")
    with pytest.raises(CloudLinkError):
        cloud_links.fetch(link, max_bytes=1024)
    # The metadata-service URL was never requested — blocked before connecting.
    assert all("169.254" not in c for c in calls)


def test_fetch_http_scheme_is_refused(monkeypatch):
    def handler(request):
        return httpx.Response(302, headers={"location": "http://www.dropbox.com/plain"})

    _mock_client(monkeypatch, handler)
    link = CloudLink("https://www.dropbox.com/s/abc/q.pdf", "q.pdf", "dropbox")
    with pytest.raises(CloudLinkError):
        cloud_links.fetch(link, max_bytes=1024)


def test_fetch_html_login_page_tries_next_candidate(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request.url.host)
        if request.url.host == "api.onedrive.com":
            return httpx.Response(200, content=PDF,
                                  headers={"content-type": "application/pdf"})
        return httpx.Response(200, content=b"<html><body>Sign in</body></html>",
                              headers={"content-type": "text/html"})

    _mock_client(monkeypatch, handler)
    link = CloudLink("https://1drv.ms/b/s!tok", "q.pdf", "onedrive")
    assert cloud_links.fetch(link, max_bytes=1024).content == PDF
    assert seen[0] == "1drv.ms" and "api.onedrive.com" in seen


def test_fetch_auth_required_wins_error_ranking(monkeypatch):
    _mock_client(monkeypatch, lambda r: httpx.Response(403))
    link = CloudLink("https://vendor-my.sharepoint.com/:b:/p/j/TOK", "q.pdf", "onedrive")
    with pytest.raises(CloudLinkError) as exc:
        cloud_links.fetch(link, max_bytes=1024)
    assert exc.value.reason == "auth_required"


def test_fetch_size_cap_aborts(monkeypatch):
    _mock_client(monkeypatch, lambda r: httpx.Response(
        200, content=b"x" * 2048, headers={"content-type": "application/pdf"}))
    link = CloudLink("https://www.dropbox.com/s/abc/q.pdf", "q.pdf", "dropbox")
    with pytest.raises(CloudLinkError) as exc:
        cloud_links.fetch(link, max_bytes=1024)
    assert exc.value.reason == "too_large"


def test_filename_extension_inferred_from_content_type(monkeypatch):
    _mock_client(monkeypatch, lambda r: httpx.Response(
        200, content=PDF, headers={"content-type": "application/pdf"}))
    link = CloudLink("https://www.dropbox.com/s/abc/quote", "quote", "dropbox")
    assert cloud_links.fetch(link, max_bytes=1024).filename == "quote.pdf"
