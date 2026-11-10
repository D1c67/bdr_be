"""Pre-persistence secret guard for chat messages and help text
(docs/ASSISTANT_BROWSER_V2_PLAN.md 7.7, 8.1, 8.2).

guard_message runs in POST /assistant/conversations/{id}/messages before
repo.insert_message. In order:

  1. a phrase that announces a password (`password:`, `pw=`, `passcode`)
     refuses the message;
  2. any word that is one of the user's saved passwords (compared through
     vault.password_fingerprint, never decrypted) refuses the message;
  3. while a credential card is open in the conversation, a `user / pass`
     pair or a lone high-entropy token refuses the message;
  4. while a code card is open (an mfa or mfa_step_up help on one of this
     conversation's browser jobs), code-shaped tokens are cut out: exactly
     one goes to that help's one-time secret path and the stored text says
     `[code]`; two or more are not sent anywhere and the user is asked to
     use the code card.

A saved password is checked before the code intercept, so a password that
happens to look like a code is refused, never typed into a site's code box.
A refused message is not stored, logged or sent to the model; nothing here
logs the text or a token (only exception class names). This is a safety
net, not a guarantee: the cards are the real path.

guard_help_text is the same net for owner help-text replies (8.1): any saved
password of this user, on any site, or any code-shaped token while an mfa or
mfa_step_up help is open for the job.

Owner: WI-4. WI-6 calls guard_help_text from assistant_browser_help.
"""
from __future__ import annotations

import logging
import math
import re
import uuid
from dataclasses import dataclass

from app.assistant import repo
from app.core.config import get_settings

logger = logging.getLogger(__name__)

SECRET_IN_MESSAGE = "secret_in_message"
SECRET_IN_MESSAGE_TEXT = "Do not type passwords in chat. Use the sign-in card."
CODE_PLACEHOLDER = "[code]"
CODE_SENT_STUB = "Code sent to the browser task"

CODE_MIN, CODE_MAX = 4, 12
PASSWORD_MIN, PASSWORD_MAX = 6, 128
# Bounds the HMAC work per message (a chat message is at most a few KB).
MAX_PASSWORD_CANDIDATES = 400
CODE_HELP_REASONS = ("mfa", "mfa_step_up")

# 7.7 item 2: phrases that announce a password.
_PASSWORD_PHRASE = re.compile(r"\b(?:passwords?|pw|pwd)\s*[:=]|\bpasscodes?\b", re.IGNORECASE)
_CHUNK = re.compile(r"[A-Za-z0-9]+")
_DIGIT = re.compile(r"[0-9]")
_TRIM = "\"'`()[]{}<>.,;:!?"
_MAX_TRIM = 3
# What may stay around a lone code for the message to count as "only the code".
_ONLY_CODE_REST = re.compile(r"^(?:(?:here\s+is\s+)?(?:the\s+|my\s+)?code(?:\s+is)?)?$")
# 7.7 item 3: "user / pass" (one word each side of one separator).
_PAIR = re.compile(r"^\s*(\S+)\s*(?:/|\||,|:|\s)\s*(\S+)\s*$")


class SecretInMessage(ValueError):
    """Raised to refuse a message (HTTP 422 secret_in_message). The text is
    not stored, logged or sent to the model."""

    code = SECRET_IN_MESSAGE

    def __init__(self, message: str = SECRET_IN_MESSAGE_TEXT) -> None:
        super().__init__(message)


@dataclass(frozen=True)
class GuardResult:
    content: str                      # what is stored and sent to the model
    intercepted_code: bool            # exactly one code went to the help path
    ask_for_code_card: bool           # two or more code-shaped tokens


# ── Code-shaped tokens (8.2) ─────────────────────────────────────────────────


def _site_regex(value: str | None) -> re.Pattern[str] | None:
    if not value or not isinstance(value, str) or len(value) > 500:
        return None
    try:
        return re.compile(value)
    except re.error:
        return None


def _code_spans(text: str, *, site_code_regex: str | None = None) -> list[tuple[int, int, str]]:
    """(start, end, code) for each code-shaped token. A token is a run of
    letters and digits, optionally split by single hyphens, or by single
    spaces between chunks that each hold a digit ("123 456"), that is 4 to
    12 characters once the separators are removed and holds at least one
    digit (or fully matches the site's own code pattern). Plain words never
    match, and a word such as "code" is never joined to the code after it."""
    if not isinstance(text, str) or not text:
        return []
    site = _site_regex(site_code_regex)
    chunks = [(m.start(), m.end(), m.group(0)) for m in _CHUNK.finditer(text)]
    out: list[tuple[int, int, str]] = []
    i = 0
    while i < len(chunks):
        start, end, joined = chunks[i]
        j = i
        while j + 1 < len(chunks):
            sep = text[chunks[j][1]:chunks[j + 1][0]]
            nxt = chunks[j + 1][2]
            joinable = sep == "-" or (sep == " " and _DIGIT.search(chunks[j][2]) and _DIGIT.search(nxt))
            if not joinable or len(joined) + len(nxt) > CODE_MAX:
                break
            joined += nxt
            end = chunks[j + 1][1]
            j += 1
        if CODE_MIN <= len(joined) <= CODE_MAX and (
                _DIGIT.search(joined) or (site is not None and site.fullmatch(joined))):
            out.append((start, end, joined))
        i = j + 1
    return out


def code_tokens(text: str, *, site_code_regex: str | None = None) -> list[str]:
    """Code-shaped tokens (8.2): 4 to 12 letters and digits after removing
    single spaces or hyphens, at least one digit (or the site regex)."""
    return [code for _, _, code in _code_spans(text, site_code_regex=site_code_regex)]


def _replace_codes(text: str, spans: list[tuple[int, int, str]]) -> str:
    out, last = [], 0
    for start, end, _ in spans:
        out.append(text[last:start])
        out.append(CODE_PLACEHOLDER)
        last = end
    out.append(text[last:])
    return "".join(out)


def _only_the_code(text: str, spans: list[tuple[int, int, str]]) -> bool:
    rest = _replace_codes(text, spans).replace(CODE_PLACEHOLDER, " ")
    rest = re.sub(r"[^A-Za-z0-9\s]", " ", rest).lower()
    return bool(_ONLY_CODE_REST.match(" ".join(rest.split())))


# ── Saved passwords (7.7 item 2) ─────────────────────────────────────────────


def _password_candidates(text: str) -> list[str]:
    """Every whitespace word (and the word with surrounding punctuation or
    quotes trimmed) of saved-password length, plus the whole message when it
    is that short. Bounded."""
    seen: list[str] = []

    def add(value: str) -> None:
        if PASSWORD_MIN <= len(value) <= PASSWORD_MAX and value not in seen:
            seen.append(value)

    whole = text.strip()
    add(whole)
    for word in text.split():
        # Remove 0 to 3 edge characters one at a time, never all of them at
        # once: a password may itself end in "!" or ")" and still be quoted
        # or followed by punctuation ('"Summer2026!"', 'Summer2026!.').
        lead = 0
        while lead < _MAX_TRIM and lead < len(word) and word[lead] in _TRIM:
            lead += 1
        trail = 0
        while trail < _MAX_TRIM and trail < len(word) - lead and word[len(word) - 1 - trail] in _TRIM:
            trail += 1
        for i in range(lead + 1):
            for k in range(trail + 1):
                add(word[i:len(word) - k])
        if len(seen) >= MAX_PASSWORD_CANDIDATES:
            break
    return seen[:MAX_PASSWORD_CANDIDATES]


def _saved_fingerprints(user_id: str) -> set[str]:
    """The fingerprints of every saved credential of this user (every site,
    revoked rows included: an old password is still a password). Read
    failures give an empty set (the phrase rules still apply)."""
    try:
        rows = repo.browser_credentials_v2_all(user_id)
    except Exception as exc:  # noqa: BLE001 - a safety net never blocks chat on a read failure
        logger.warning("browser secret guard: credential read failed (%s)", type(exc).__name__)
        return set()
    return {str(r["fingerprint"]) for r in rows or () if isinstance(r, dict) and r.get("fingerprint")}


def _holds_saved_password(user_id: str, text: str) -> bool:
    fingerprints = _saved_fingerprints(user_id)
    if not fingerprints:
        return False
    from app.services import assistant_browser_vault as vault

    try:
        for candidate in _password_candidates(text):
            if vault.password_fingerprint(user_id, candidate) in fingerprints:
                return True
    except vault.VaultUnavailable:
        return False
    return False


# ── The credential card shapes (7.7 item 3) ──────────────────────────────────


def _classes(value: str) -> int:
    return sum((any(c.islower() for c in value), any(c.isupper() for c in value),
                any(c.isdigit() for c in value), any(not c.isalnum() for c in value)))


def _entropy(value: str) -> float:
    counts: dict[str, int] = {}
    for c in value:
        counts[c] = counts.get(c, 0) + 1
    return -sum(n / len(value) * math.log2(n / len(value)) for n in counts.values())


def _password_like(value: str) -> bool:
    if not 6 <= len(value) <= PASSWORD_MAX or "://" in value or value.lower().startswith("www."):
        return False
    return _classes(value) >= 2 and (any(c.isdigit() for c in value) or not value.isalpha())


def _lone_secret_token(text: str) -> bool:
    value = text.strip()
    if not 8 <= len(value) <= PASSWORD_MAX or any(c.isspace() for c in value):
        return False
    if "://" in value or value.lower().startswith("www.") or re.fullmatch(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}", value):
        return False
    return _classes(value) >= 3 or (_classes(value) >= 2 and len(value) >= 12 and _entropy(value) >= 3.0)


def _credential_shaped(text: str) -> bool:
    """`user / pass`, `user:pass` or `user pass` with a password-like second
    word, or a lone high-entropy token."""
    match = _PAIR.match(text)
    if match:
        left, right = match.group(1), match.group(2)
        if _password_like(right) and ("@" in left or len(left) >= 3):
            return True
    return _lone_secret_token(text)


# ── Open cards ───────────────────────────────────────────────────────────────


def _credential_card_open(user_id: str, conversation_id: str) -> bool:
    try:
        return bool(repo.browser_requests_open(user_id, conversation_id=conversation_id, kind="credential", limit=1))
    except Exception as exc:  # noqa: BLE001 - a safety net never blocks chat on a read failure
        logger.warning("browser secret guard: request read failed (%s)", type(exc).__name__)
        return False


def _open_code_helps(user_id: str, conversation_id: str) -> list[dict]:
    try:
        return list(repo.browser_open_code_helps(user_id, conversation_id) or ())
    except Exception as exc:  # noqa: BLE001 - a safety net never blocks chat on a read failure
        logger.warning("browser secret guard: help read failed (%s)", type(exc).__name__)
        return []


def _site_code_regex(user_id: str, site_key: str | None) -> str | None:
    if not site_key:
        return None
    try:
        site = repo.browser_site(user_id, str(site_key))
    except Exception:  # noqa: BLE001 - the generic code shape still applies
        return None
    value = (site or {}).get("code_email_regex")
    return value if isinstance(value, str) else None


def _deliver_code(user_id: str, help_row: dict, code: str) -> bool:
    """The code goes to the same one-time secret path the code card uses
    (assistant_browser_help.owner_reply with a secret value). False when the
    help changed or the path refused it; the code is never logged."""
    from app.services import assistant_browser_help as help_flow

    try:
        help_flow.owner_reply(user_id, str(help_row["job_id"]), expected_help_id=str(help_row["help_id"]),
                              action="message", client_msg_id=str(uuid.uuid4()), secret_value=code)
        return True
    except Exception as exc:  # noqa: BLE001 - the stored text says [code]; the card stays the path
        logger.warning("browser secret guard: code delivery refused (%s)", type(exc).__name__)
        return False


# ── Public entry points ──────────────────────────────────────────────────────


def _guard_enabled() -> bool:
    settings = get_settings()
    return bool(getattr(settings, "assistant_browser_enabled", False)
                or getattr(settings, "assistant_browser_v2_enabled", False))


def guard_message(user_id: str, conversation_id: str, content: str) -> GuardResult:
    """Run before repo.insert_message in POST /assistant/messages. Raises
    SecretInMessage; returns the content to store with any code replaced by
    [code] (or the stub "Code sent to the browser task")."""
    if not isinstance(content, str) or not content or not _guard_enabled():
        return GuardResult(content=content, intercepted_code=False, ask_for_code_card=False)
    if _PASSWORD_PHRASE.search(content):
        raise SecretInMessage()
    if _holds_saved_password(user_id, content):
        raise SecretInMessage()
    helps = _open_code_helps(user_id, conversation_id)
    site_regex = _site_code_regex(user_id, helps[0].get("site_key")) if len(helps) == 1 else None
    spans = _code_spans(content, site_code_regex=site_regex) if helps else []
    if _credential_card_open(user_id, conversation_id):
        # A lone code while a code card is open is the code, not a password.
        lone_code = len(spans) == 1 and _only_the_code(content, spans)
        if not lone_code and _credential_shaped(content):
            raise SecretInMessage()
    if not spans:
        return GuardResult(content=content, intercepted_code=False, ask_for_code_card=False)
    masked = _replace_codes(content, spans)
    if len(spans) > 1 or len(helps) != 1:
        # Two codes, or two tasks waiting for one: nothing is sent.
        return GuardResult(content=masked, intercepted_code=False, ask_for_code_card=True)
    if not _deliver_code(user_id, helps[0], spans[0][2]):
        return GuardResult(content=masked, intercepted_code=False, ask_for_code_card=True)
    stored = CODE_SENT_STUB if _only_the_code(content, spans) else masked
    return GuardResult(content=stored, intercepted_code=True, ask_for_code_card=False)


def guard_help_text(user_id: str, job_id: str, text: str) -> None:
    """Owner help-text replies (8.1): raises SecretInMessage on any saved
    password fingerprint, or any code-shaped token while mfa or mfa_step_up
    help is open for the job."""
    if not isinstance(text, str) or not text:
        return
    if _PASSWORD_PHRASE.search(text) or _holds_saved_password(user_id, text):
        raise SecretInMessage()
    try:
        state = repo.browser_help_state(user_id, job_id)
    except Exception as exc:  # noqa: BLE001 - fail closed: the help path is a secret path
        logger.warning("browser secret guard: help state read failed (%s)", type(exc).__name__)
        state = {"status": "open", "reason_code": "mfa"}
    if state and state.get("status") == "open" and state.get("reason_code") in CODE_HELP_REASONS:
        if code_tokens(text):
            raise SecretInMessage("Use the code field on the card for a sign-in code.")
