"""Encoded MFA values never become model input or private trace text."""
import base64
import hashlib
import hmac

import pytest

from app.services.assistant_browser_redaction import scrub_codes

KEY = b"private-owner-vault-key"


def digest(code, key=KEY):
    return hmac.new(key, code.encode(), hashlib.sha256).hexdigest()


@pytest.mark.parametrize("code", ["1234", "012345", "123456789012"])
def test_supported_codes_match_plain_embedded_and_encoded_forms(code):
    encoded = base64.b64encode(code.encode()).decode()
    values = [code, "otp" + code, encoded, encoded.rstrip("="),
              "".join(f"%{ord(char):02X}" for char in code)]
    clean = scrub_codes(values, KEY, [digest(code)])
    assert clean == ["[redacted]", "otp[redacted]", "[redacted]", "[redacted]", "[redacted]"]


def test_mixed_url_encoding_keys_and_numeric_observations_are_scrubbed():
    code = "123456"
    value = {code: [123456, "%31" + code[1:], {"otp": "otp123456"}]}
    assert scrub_codes(value, KEY, [digest(code)]) == {
        "[redacted]": ["[redacted]", "[redacted]", {"otp": "otp[redacted]"}]}


def test_unrelated_ids_other_owner_hashes_and_malformed_base64_are_unchanged():
    value = {"url": "https://app.procore.com/654321/project/rfi/show/123456789",
             "ordinary": [654321, True, "not-a-code", "MTIzNDU2!", "%%wrong", "a" * 10000]}
    assert scrub_codes(value, KEY, [digest("111111")]) == value
    assert scrub_codes("otp123456 MTIzNDU2 %31%32%33%34%35%36", KEY,
                       [digest("123456", b"another-owner-key")]) == "otp123456 MTIzNDU2 %31%32%33%34%35%36"


def test_large_noncode_base64_is_not_decoded_as_unbounded_input():
    text = base64.b64encode(b"1" * 10000).decode()
    assert scrub_codes(text, KEY, [digest("111111")]) == text
