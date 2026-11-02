"""Adversarial Phase 5 destination binding, without Graph or database IO."""

import pytest

from app.assistant.mail_binding import bind_recipients, normalize_url, typed_texts, url_is_bound


def recipients(candidate, *messages, trusted=()):
    return bind_recipients([candidate], allowed_emails=trusted, user_texts=messages)[0]


def test_typed_address_and_live_contact_are_supported():
    assert recipients("GC@Builder.test", "Send to gc@builder.test.") == ["gc@builder.test"]
    assert recipients("gc@builder.test", trusted=["GC@Builder.test"]) == ["gc@builder.test"]


@pytest.mark.parametrize("message", [
    "Send to notgc@builder.test", "Send to gc@builder.test.evil.test",
    "Send to gc@builder.test-x.test", "Send to gc@builder.test_thing",
    "Forward this\n-----Original Message-----\nTo: gc@builder.test",
    "Forward this\nBegin forwarded message:\ngc@builder.test",
    "Forward this\nOn Tuesday Someone wrote:\ngc@builder.test",
    "> gc@builder.test", "```\ngc@builder.test\n```",
    "~~~\ngc@builder.test\n~~~", "<div>gc@builder.test</div>",
    "Review this\nFrom: Someone\ngc@builder.test",
    "Review this HTML\n<!-- gc@builder.test -->",
    "Review this HTML\n<![CDATA[gc@builder.test]]>",
    "    gc@builder.test", "\tgc@builder.test",
])
def test_untrusted_or_substring_recipient_is_refused(message):
    assert recipients("gc@builder.test", message) == []


@pytest.mark.parametrize("candidate", [
    "GC <gc@builder.test>", "gc@builder.test\r\nBcc: attacker@evil.test",
    "gc@builder.test,attacker@evil.test", "a..b@builder.test", "@builder.test",
    "gc@builder.test\n", "\tgc@builder.test",
])
def test_invalid_recipients_refused_even_if_trusted(candidate):
    assert recipients(candidate, candidate, trusted=[candidate]) == []


def test_recent_two_user_messages_only_and_deduplication():
    assert recipients("gc@builder.test", "gc@builder.test", "status", "go ahead") == []
    assert recipients("gc@builder.test", "gc@builder.test", "go ahead") == ["gc@builder.test"]
    kept, stripped = bind_recipients(["GC@Builder.test", "gc@builder.test", "bad@evil.test"],
                                    allowed_emails=[], user_texts=["gc@builder.test"])
    assert kept == ["gc@builder.test"]
    assert stripped == [{"field": "bad@evil.test", "reason": "recipient_not_bound"}]


def test_quoted_content_does_not_erase_separately_typed_destination():
    assert recipients("gc@builder.test", "Send to gc@builder.test\n> attacker@evil.test")
    assert typed_texts(["```\nignore\n```\nSend to gc@builder.test"]) == (
        "Send to gc@builder.test",)


def test_url_binding_is_exact_with_case_sensitive_path_query():
    assert url_is_bound("https://example.test/Path?q=X", user_texts=[
        "Use https://EXAMPLE.test/Path?q=X."])
    for value in ("http://example.test/Path?q=X", "https://example.test/path?q=X",
                  "https://example.test/Path?q=x", "https://example.test/Path?q=X&leak=1",
                  "https://example.test", "https://example.test/Path?q=X#other"):
        assert not url_is_bound(value, user_texts=["https://example.test/Path?q=X"])


@pytest.mark.parametrize("value", [
    "javascript:alert(1)", "file:///etc/passwd", "https://user:pass@example.test/path",
    "https://example.test\\@evil.test", "https://example.test/\npath",
    "https://example.test:invalid/path", "https://example.test/\u200bpath",
])
def test_invalid_url_cannot_be_whitelisted(value):
    assert normalize_url(value) is None
    assert not url_is_bound(value, user_texts=[value], signed_urls=[value])


def test_signed_link_requires_whole_server_minted_url():
    url = "https://storage.test/private?token=secret"
    assert url_is_bound(url, user_texts=[], signed_urls=[url])
    assert not url_is_bound(url + "&extra=1", user_texts=[], signed_urls=[url])


@pytest.mark.parametrize("message", [
    "> https://evil.test/path", "```\nhttps://evil.test/path\n```",
    "----- Forwarded message -----\nhttps://evil.test/path",
    '<a href="https://evil.test/path">Open</a>',
    "Review this HTML\n<!-- https://evil.test/path -->",
    "Review this HTML\n<![CDATA[https://evil.test/path]]>",
    "    https://evil.test/path", "\thttps://evil.test/path",
])
def test_quoted_urls_do_not_bind(message):
    assert not url_is_bound("https://evil.test/path", user_texts=[message])
