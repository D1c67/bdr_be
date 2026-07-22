"""HTML sanitization for stored rich text (RFI questions, migration 0068).

THIS MODULE IS THE SECURITY BOUNDARY FOR STORED HTML. The frontend renders these
values with `dangerouslySetInnerHTML` and there is NO client-side sanitizer, so
every rich-text value MUST be sanitized HERE — server-side, on WRITE, always.
Whatever reaches the database is treated by the UI as safe to inject into the DOM
verbatim; nothing downstream gets a second chance to catch a payload. Sanitizing
on read instead would leave the stored row hostile to every other consumer (ZIP
exports, emails, a future mobile client), and sanitizing in the browser would put
the check on the wrong side of the trust line.

The allowlist is exactly what the Tiptap editor can emit and nothing more. It is
deliberately not a "reasonable HTML subset": no `img` (no remote loads/beacons),
no `script`/`style`, no `class`/`id`/`style` attributes (no way to reach into the
app's own CSS), no `iframe`/`object`/`embed`. Widening it means widening the
attack surface — add a tag here only when the editor can actually produce it.
"""

import html as html_lib

import nh3

# Exactly the tags Tiptap emits. Anything else is stripped while its text
# content survives (`<div>hi</div>` → `hi`), so a paste from Word/Outlook loses
# its markup, not its words.
_ALLOWED_TAGS: frozenset[str] = frozenset(
    {
        "p", "br",
        "strong", "em", "u", "s",
        "ul", "ol", "li",
        "h3", "h4",
        "blockquote", "code", "pre",
        "a",
    }
)

# Links only, and only the two attributes a link needs. `rel`/`target` are NOT
# listed: they are forced below, so an author-supplied `target="_self"` or
# `rel="me"` is overwritten rather than honored.
_ALLOWED_ATTRIBUTES: dict[str, set[str]] = {"a": {"href", "title"}}

# Narrower than nh3's default scheme list (which includes ftp, irc, magnet, …).
# `javascript:` and `data:` are absent from both — an href with an unlisted
# scheme is dropped and the anchor is left inert.
_URL_SCHEMES: set[str] = {"http", "https", "mailto", "tel"}

# Every link leaves the app in a new tab and cannot reach back through
# `window.opener` (reverse tabnabbing) or leak the referrer.
_LINK_REL = "noopener noreferrer"
_LINK_TARGET = {"a": {"target": "_blank"}}

# `script`/`style` are dropped *with* their contents (nh3's default) rather than
# stripped-but-kept: `alert(1)` surfacing as visible prose helps nobody.
_CLEAN_CONTENT_TAGS: set[str] = {"script", "style"}


def sanitize_rich_text(html: str) -> str:
    """Reduce untrusted HTML to the editor's allowlist. Call on WRITE, always.

    Not idempotent-hostile: re-sanitizing an already-clean value is a no-op, so
    it is safe to run over a value of unknown provenance.
    """
    return nh3.clean(
        html or "",
        tags=set(_ALLOWED_TAGS),
        clean_content_tags=_CLEAN_CONTENT_TAGS,
        attributes=_ALLOWED_ATTRIBUTES,
        url_schemes=_URL_SCHEMES,
        link_rel=_LINK_REL,
        set_tag_attribute_values=_LINK_TARGET,
        strip_comments=True,
    )


def rich_text_to_text(html: str) -> str:
    """The visible text of a rich-text value, entities resolved and trimmed.

    For emptiness checks and any plain-text rendering (search, digests). `<p><br>
    </p>` and `<p>&nbsp;</p>` — what an "empty" editor actually posts — both come
    back as "".
    """
    stripped = nh3.clean(
        html or "",
        tags=set(),
        clean_content_tags=_CLEAN_CONTENT_TAGS,
        attributes={},
        strip_comments=True,
    )
    # `&nbsp;` → U+00A0, which str.strip() removes (it is Unicode whitespace).
    return html_lib.unescape(stripped).strip()


def has_text_content(html: str) -> bool:
    """False for markup that renders to nothing — `<p><br></p>`, `<p>&nbsp;</p>`,
    a lone `<script>`. Guards "required" rich-text fields, which a min_length on
    the raw HTML cannot: `<p></p>` is 7 characters of nothing."""
    return bool(rich_text_to_text(html))
