"""Regenerate the animated RFQ documents card (app/assets/rfq-documents-card.gif).

RFQ emails whose drawings/specs are too large to attach carry a SharePoint
folder link, and vendors were skimming past it. The email now leads with this
card: an amber box with an "Open Drawings & Specs" button and a light that
travels around its border. Email clients strip CSS animation (Gmail, Outlook),
so the animation has to be a GIF; the card text never changes (the link lives
on the <a> around the image), so one prebuilt asset serves every send.

    python scripts/make_rfq_documents_card.py

Needs Pillow + numpy (a one-off asset build, not a runtime dependency).
"""

import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

ASSETS = Path(__file__).resolve().parent.parent / "app" / "assets"
OUTPUT = ASSETS / "rfq-documents-card.gif"
FONT_DIR = Path("/System/Library/Fonts/Supplemental")

S = 2  # render at 2x; the email shows it at WIDTH x HEIGHT CSS px
WIDTH, HEIGHT = 536, 190  # must match email_branding.DOCUMENTS_CARD_SIZE
INSET = 8  # room outside the border for the glow
RADIUS = 12
FRAMES = 80
FRAME_MS = 20  # 1.6 s per lap at 50 fps (GIF delays under 20 ms get clamped to 100 ms)

PAGE = (255, 255, 255)
CARD_BG = (255, 246, 219)  # #fff6db
BORDER = (240, 200, 90)  # #f0c85a
GLOW = (255, 150, 0)  # the travelling light
LABEL = (138, 106, 0)
NAVY = (32, 33, 89)
TEXT = (42, 45, 52)


def font(name: str, px: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_DIR / name), px * S)


def rounded_path(x0, y0, x1, y1, r, step=0.5):
    """Points every `step` px along a rounded rectangle, clockwise from the
    top-left corner's end."""
    pts = []

    def line(ax, ay, bx, by):
        n = max(1, int(math.hypot(bx - ax, by - ay) / step))
        pts.extend((ax + (bx - ax) * i / n, ay + (by - ay) * i / n) for i in range(n))

    def arc(cx, cy, a0):
        n = max(1, int(math.pi / 2 * r / step))
        pts.extend(
            (cx + r * math.cos(a0 + math.pi / 2 * i / n), cy + r * math.sin(a0 + math.pi / 2 * i / n))
            for i in range(n)
        )

    line(x0 + r, y0, x1 - r, y0)
    arc(x1 - r, y0 + r, -math.pi / 2)
    line(x1, y0 + r, x1, y1 - r)
    arc(x1 - r, y1 - r, 0)
    line(x1 - r, y1, x0 + r, y1)
    arc(x0 + r, y1 - r, math.pi / 2)
    line(x0, y1 - r, x0, y0 + r)
    arc(x0 + r, y0 + r, math.pi)
    return np.array(pts)


def base_card() -> Image.Image:
    w, h = WIDTH * S, HEIGHT * S
    im = Image.new("RGB", (w, h), PAGE)
    d = ImageDraw.Draw(im)
    box = (INSET * S, INSET * S, w - INSET * S, h - INSET * S)
    d.rounded_rectangle(box, RADIUS * S, fill=CARD_BG, outline=BORDER, width=2 * S)

    x = (INSET + 22) * S
    y = (INSET + 20) * S
    # Letter-spaced label.
    f = font("Arial Bold.ttf", 11)
    for ch in "PROJECT DOCUMENTS":
        d.text((x, y), ch, font=f, fill=LABEL)
        x += d.textlength(ch, font=f) + 2 * S
    x = (INSET + 22) * S
    d.text((x, y + 20 * S), "Drawings & specifications for this bid", font=font("Arial Bold.ttf", 19), fill=NAVY)
    d.text(
        (x, y + 50 * S),
        "Too large to attach, so they are in a shared SharePoint folder. No login needed.",
        font=font("Arial.ttf", 13),
        fill=TEXT,
    )
    # Button.
    bf = font("Arial Bold.ttf", 16)
    label = "Open Drawings & Specs  →"
    bw = d.textlength(label, font=bf) + 60 * S
    by = y + 80 * S
    d.rounded_rectangle((x, by, x + bw, by + 46 * S), 8 * S, fill=NAVY)
    d.text((x + 30 * S, by + 23 * S), label, font=bf, fill=(255, 255, 255), anchor="lm")
    return im


def main() -> None:
    base = base_card()
    arr = np.asarray(base).astype(np.float32)
    h, w = arr.shape[:2]

    # Border centre line; each nearby pixel gets its distance to the line and
    # its position along it, so a frame is just a function of "where the head is".
    half = S  # border is 2*S wide, centred on the path
    path = rounded_path(INSET * S + half, INSET * S + half, w - INSET * S - half, h - INSET * S - half, RADIUS * S)
    seg = np.r_[0, np.cumsum(np.hypot(*np.diff(path, axis=0).T))]
    perim = seg[-1] + np.hypot(*(path[0] - path[-1]))

    band = 12 * S
    ys, xs = np.mgrid[0:h, 0:w]
    near = np.zeros((h, w), bool)
    near[: (INSET * S + band), :] = True
    near[h - (INSET * S + band) :, :] = True
    near[:, : (INSET * S + band)] = True
    near[:, w - (INSET * S + band) :] = True
    py, px = ys[near], xs[near]
    dist = np.empty(py.size, np.float32)
    pos = np.empty(py.size, np.float32)
    for i in range(0, py.size, 4000):
        dy = py[i : i + 4000, None] - path[None, :, 1]
        dx = px[i : i + 4000, None] - path[None, :, 0]
        d2 = dx * dx + dy * dy
        j = d2.argmin(1)
        dist[i : i + 4000] = np.sqrt(d2[np.arange(j.size), j])
        pos[i : i + 4000] = seg[j]

    tail = perim * 0.28
    glow = np.array(GLOW, np.float32)
    rgb_frames = []
    for k in range(FRAMES):
        head = perim * k / FRAMES
        behind = (head - pos) % perim  # 0 at the head, growing along the tail
        ahead = perim - behind  # distance in front of the head
        along = np.where(
            ahead < 4 * S,
            np.clip(1 - ahead / (4 * S), 0, 1) ** 2,  # soft rounded nose
            np.clip(1 - behind / tail, 0, 1) ** 1.6,
        )
        core = np.clip(1.6 * S - dist, 0, 1)  # solid on the border itself
        halo = np.exp(-((dist / (4.5 * S)) ** 2)) * 0.55
        a = (along * np.maximum(core, halo))[:, None]
        # Hot white-gold tip right at the head.
        tip = (np.clip(1 - np.minimum(behind, ahead) / (perim * 0.015), 0, 1) * np.maximum(core, halo) * 0.7)[:, None]
        frame = arr.copy()
        pix = frame[py, px]
        pix = pix * (1 - a) + glow * a
        pix = pix * (1 - tip) + 255 * tip
        frame[py, px] = pix
        rgb_frames.append(Image.fromarray(frame.clip(0, 255).astype(np.uint8)))

    # One shared palette so frames never shimmer.
    sample = Image.new("RGB", (w, h * 2))
    sample.paste(rgb_frames[0], (0, 0))
    sample.paste(rgb_frames[FRAMES // 3], (0, h))
    pal = sample.quantize(64, method=Image.Quantize.MEDIANCUT)
    frames = [f.quantize(palette=pal, dither=Image.Dither.NONE) for f in rgb_frames]
    frames[0].save(
        OUTPUT, save_all=True, append_images=frames[1:], duration=FRAME_MS, loop=0, optimize=True, disposal=1
    )
    print(f"Wrote {OUTPUT} ({OUTPUT.stat().st_size // 1024} KB, {w}x{h}, {FRAMES} frames)")


if __name__ == "__main__":
    main()
