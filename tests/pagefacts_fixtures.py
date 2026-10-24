"""Synthetic PDF builder for the page-facts tests (WS1).

`make_pdf([sheet(...), ...])` assembles a raw PDF (no pypdf object model, so
every page detail is explicit): Helvetica text at inch positions measured
from the top-left of the displayed page, optional /Rotate, CropBox offsets,
invisible text (render mode 3), an image XObject covering a share of the
page, N tiny filled paths (vector-text pages), and "AutoCAD SHX Text"
comment annotations. pdfium's charboxes come from the built-in Helvetica
metrics, so word positions are deterministic.

Coordinates given to `sheet()` are in the DISPLAYED frame (after rotation,
inside the crop box, origin top-left, inches); the builder maps them back
into PDF user space so the tests can assert the 5.1 transform.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

PT = 72.0


@dataclass(slots=True)
class Text:
    x_in: float  # left edge of the text in the displayed frame
    y_in: float  # baseline from the top of the displayed frame
    size: float
    text: str
    angle: float = 0.0  # counter-clockwise degrees in the displayed frame
    invisible: bool = False
    bold: bool = False


@dataclass(slots=True)
class Shx:
    x_in: float
    y_in: float
    w_in: float
    h_in: float
    text: str


@dataclass(slots=True)
class Sheet:
    w_in: float = 42.0
    h_in: float = 30.0
    rotate: int = 0
    crop_offset_in: tuple[float, float] = (
        0.0,
        0.0,
    )  # (dx, dy) of the crop box inside a larger media box
    texts: list[Text] = field(default_factory=list)
    image_cover: float = 0.0
    paths: int = 0
    shx: list[Shx] = field(default_factory=list)


def sheet(
    *texts: tuple,
    w_in: float = 42.0,
    h_in: float = 30.0,
    rotate: int = 0,
    crop_offset_in: tuple[float, float] = (0.0, 0.0),
    image_cover: float = 0.0,
    paths: int = 0,
    shx: list[Shx] | None = None,
) -> Sheet:
    """`texts` are (x_in, y_in, size_pt, text[, angle_deg[, invisible[, bold]]])."""
    out = Sheet(
        w_in=w_in,
        h_in=h_in,
        rotate=rotate,
        crop_offset_in=crop_offset_in,
        image_cover=image_cover,
        paths=paths,
        shx=list(shx or []),
    )
    for t in texts:
        x, y, size, text = t[:4]
        angle = t[4] if len(t) > 4 else 0.0
        invisible = bool(t[5]) if len(t) > 5 else False
        bold = bool(t[6]) if len(t) > 6 else False
        out.texts.append(Text(x, y, size, text, angle, invisible, bold))
    return out


def _escape(text: str) -> bytes:
    return (
        text.encode("latin-1", errors="replace")
        .replace(b"\\", b"\\\\")
        .replace(b"(", b"\\(")
        .replace(b")", b"\\)")
    )


def _displayed_to_user(s: Sheet, x_in: float, y_in: float) -> tuple[float, float]:
    """Displayed-frame inches (origin top-left) to PDF user space points
    inside the crop box, inverting /Rotate (the 5.1 transform)."""
    u, v = x_in / s.w_in, y_in / s.h_in
    rot = s.rotate % 360
    if rot == 90:  # display (1 - v, u) = (u', v') -> u = v', v = 1 - u'
        u, v = v, 1 - u
    elif rot == 180:
        u, v = 1 - u, 1 - v
    elif rot == 270:
        u, v = 1 - v, u
    cw_in, ch_in = (s.w_in, s.h_in) if rot in (0, 180) else (s.h_in, s.w_in)
    dx, dy = s.crop_offset_in
    x_pt = (dx + u * cw_in) * PT
    y_pt = (dy + (1 - v) * ch_in) * PT
    return x_pt, y_pt


def _content(s: Sheet, image_name: bytes | None) -> bytes:
    rot = s.rotate % 360
    cw_in, ch_in = (s.w_in, s.h_in) if rot in (0, 180) else (s.h_in, s.w_in)
    dx, dy = s.crop_offset_in
    parts: list[bytes] = []
    if image_name and s.image_cover > 0:
        w = cw_in * PT * math.sqrt(s.image_cover)
        h = ch_in * PT * math.sqrt(s.image_cover)
        parts.append(
            b"q %.2f 0 0 %.2f %.2f %.2f cm /%s Do Q" % (w, h, dx * PT, dy * PT, image_name)
        )
    for i in range(s.paths):
        x = dx * PT + 10 + (i % 50) * 4
        y = dy * PT + 10 + (i // 50) * 4
        parts.append(b"%.1f %.1f 2 2 re f" % (x, y))
    for t in s.texts:
        x, y = _displayed_to_user(s, t.x_in, t.y_in)
        # The displayed page is the user page turned clockwise by /Rotate, so
        # a displayed counter-clockwise angle is that much larger in user space.
        theta = math.radians(t.angle + rot)
        c, sn = math.cos(theta), math.sin(theta)
        font = b"/F2" if t.bold else b"/F1"
        mode = b"3 Tr " if t.invisible else b"0 Tr "
        parts.append(
            b"BT %s %.2f Tf %s%.4f %.4f %.4f %.4f %.2f %.2f Tm (%s) Tj ET"
            % (font, t.size, mode, c, sn, -sn, c, x, y, _escape(t.text))
        )
    return b"\n".join(parts)


def _assemble(objects: list[bytes]) -> bytes:
    out = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n" % (len(objects) + 1)
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)


def _stream(dict_body: bytes, data: bytes) -> bytes:
    return b"<< " + dict_body + b" /Length %d >>\nstream\n" % len(data) + data + b"\nendstream"


def make_pdf(sheets: list[Sheet]) -> bytes:
    """One PDF with one page per sheet."""
    objects: list[bytes] = []
    # 1 catalog, 2 pages, 3 F1, 4 F2, 5 image; pages follow.
    objects.append(b"")  # catalog placeholder
    objects.append(b"")  # pages placeholder
    objects.append(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
    )
    objects.append(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>"
    )
    objects.append(
        _stream(
            b"/Type /XObject /Subtype /Image /Width 2 /Height 2 /ColorSpace /DeviceGray /BitsPerComponent 8",
            b"\x80\x80\x80\x80",
        )
    )
    page_ids: list[int] = []
    for s in sheets:
        rot = s.rotate % 360
        cw_in, ch_in = (s.w_in, s.h_in) if rot in (0, 180) else (s.h_in, s.w_in)
        dx, dy = s.crop_offset_in
        media = (0.0, 0.0, (dx + cw_in + dx) * PT, (dy + ch_in + dy) * PT)
        crop = (dx * PT, dy * PT, (dx + cw_in) * PT, (dy + ch_in) * PT)
        content_id = len(objects) + 2
        annot_ids: list[int] = []
        annots: list[bytes] = []
        for a in s.shx:
            x0, y0 = _displayed_to_user(s, a.x_in, a.y_in + a.h_in)
            x1, y1 = _displayed_to_user(s, a.x_in + a.w_in, a.y_in)
            rect = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
            annots.append(
                b"<< /Type /Annot /Subtype /Square /Rect [%.2f %.2f %.2f %.2f] /T (AutoCAD SHX Text) /Subj (AutoCAD SHX Text) /Contents (%s) /F 4 >>"
                % (rect[0], rect[1], rect[2], rect[3], _escape(a.text))
            )
        page = (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [%.2f %.2f %.2f %.2f] /CropBox [%.2f %.2f %.2f %.2f] /Rotate %d"
            % (media[0], media[1], media[2], media[3], crop[0], crop[1], crop[2], crop[3], rot)
            + b" /Resources << /Font << /F1 3 0 R /F2 4 0 R >> /XObject << /Im1 5 0 R >> >> /Contents %d 0 R"
            % content_id
        )
        if annots:
            first = content_id + 1
            annot_ids = list(range(first, first + len(annots)))
            page += b" /Annots [" + b" ".join(b"%d 0 R" % i for i in annot_ids) + b"]"
        page += b" >>"
        page_ids.append(len(objects) + 1)
        objects.append(page)
        objects.append(_stream(b"", _content(s, b"Im1")))
        objects.extend(annots)
    objects[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[1] = (
        b"<< /Type /Pages /Kids ["
        + b" ".join(b"%d 0 R" % i for i in page_ids)
        + b"] /Count %d >>" % len(page_ids)
    )
    return _assemble(objects)


# Ready-made sheets ---------------------------------------------------------------


def title_block_texts(
    number: str, title: str, *, w_in: float = 42.0, h_in: float = 30.0, title2: str | None = None
) -> list[tuple]:
    """An IMEG-style right-strip title block: SHEET TITLE label, the title
    (one or two lines), SHEET NO. label and a 36 pt sheet number at the
    bottom-right corner."""
    x = w_in - 3.5
    out = [
        (x, h_in - 2.4, 7, "SHEET TITLE:"),
        (x + 0.6, h_in - 2.1, 9, title),
        (x, h_in - 1.4, 7, "SHEET NO."),
        (x + 1.2, h_in - 0.6, 36, number),
        (x, 18.0, 7, "KEY PLAN"),
        (x, 21.5, 9, "REVISION"),
        (x + 2.0, 21.5, 9, "ISSUE DATE"),
    ]
    if title2:
        out.append((x + 0.6, h_in - 1.9, 9, title2))
    return out


def plan_sheet(
    number: str,
    title: str,
    scale: str = '1/4" = 1\'-0"',
    *,
    bubble: str | None = "1",
    extra: list[tuple] | None = None,
) -> Sheet:
    """A plan sheet: view title, bubble, scale under the title, a few plan
    tags and the title block."""
    texts: list[tuple] = [
        (15.0, 28.6, 18, title),
        (15.0, 29.0, 7, scale),
        (34.5, 0.9, 7, "SHEET NOTES:"),
        (34.5, 1.2, 7, "1. ALL WORK PER NEC."),
        (34.5, 1.5, 7, "2. VERIFY IN FIELD."),
    ]
    if bubble:
        texts.append((14.4, 28.9, 18, bubble))
    for i in range(12):
        texts.append((5.0 + i * 2.0, 10.0 + (i % 3), 7, f"A/1LI-{2 + i}"))
    texts.extend(title_block_texts(number, title))
    texts.extend(extra or [])
    return sheet(*texts)
