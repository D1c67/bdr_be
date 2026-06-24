"""Regenerate the email-optimized G3 logo (app/assets/g3-logo-email.jpg).

The brand source art is ~1600px / ~190 KB, but every branded email renders the
logo at just 88px in the signature and inlines it per recipient. We ship a
downscaled ~264px (3x for retina) JPEG so each send carries ~12 KB instead of
~190 KB. Run this whenever the source logo changes:

    python scripts/make_email_logo.py

Needs Pillow (not a runtime dependency — this is a one-off asset build):
    pip install pillow            # or: uvx --with pillow python scripts/make_email_logo.py
"""

from pathlib import Path

from PIL import Image

ASSETS = Path(__file__).resolve().parent.parent / "app" / "assets"
SOURCE = ASSETS / "Logo design for light bg - Copy.jpg"
OUTPUT = ASSETS / "g3-logo-email.jpg"
TARGET_WIDTH = 264  # rendered at 88px; 3x for high-DPI displays


def main() -> None:
    im = Image.open(SOURCE).convert("RGB")
    w, h = im.size
    im = im.resize((TARGET_WIDTH, round(h * TARGET_WIDTH / w)), Image.LANCZOS)
    im.save(OUTPUT, "JPEG", quality=88, optimize=True, progressive=True)
    print(f"Wrote {OUTPUT} ({OUTPUT.stat().st_size} bytes, {im.size[0]}x{im.size[1]})")


if __name__ == "__main__":
    main()
