"""Generate the frontend G3 logo assets from the brand source art.

Produces:
  bdr_be/app/assets/g3-logo-transparent.png — the brand logo with the white
                        background removed (the canonical no-background version).
  bdr_fe/public/g3-logo.png  — transparent background, for placing on the app's
                        light surfaces / white tiles.
  bdr_fe/app/icon.png        — 256px transparent favicon / application icon.

The white background is removed with a conservative border flood-fill so the
metallic silver "3" and red wires are never eroded. Run whenever the source
logo changes:

    uvx --with pillow --with numpy python scripts/make_app_logo.py
"""

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent.parent
SOURCE = ROOT / "bdr_be" / "app" / "assets" / "Logo design for light bg - Copy.jpg"
ASSET_TRANSPARENT = ROOT / "bdr_be" / "app" / "assets" / "g3-logo-transparent.png"
PUBLIC_PNG = ROOT / "bdr_fe" / "public" / "g3-logo.png"
FAVICON = ROOT / "bdr_fe" / "app" / "icon.png"

SENTINEL = (255, 0, 255)  # color the background can't contain
THRESH = 18               # only flood true/near white; protects the silver "3"
WEB_WIDTH = 512
FAVICON_SIZE = 256


def make_transparent(src: Image.Image) -> Image.Image:
    """Return an RGBA copy with border-connected white made transparent."""
    rgb = src.convert("RGB")
    painted = rgb.copy()
    w, h = painted.size
    for corner in [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]:
        ImageDraw.floodfill(painted, corner, SENTINEL, thresh=THRESH)

    bg = np.all(np.asarray(painted) == SENTINEL, axis=-1)
    alpha = np.where(bg, 0, 255).astype("uint8")
    out = np.dstack([np.asarray(rgb), alpha])
    return Image.fromarray(out, "RGBA")


def main() -> None:
    src = Image.open(SOURCE)
    w, h = src.size

    transparent = make_transparent(src)

    # Canonical no-background version saved back into the brand assets.
    transparent.save(ASSET_TRANSPARENT, "PNG", optimize=True)
    print(f"Wrote {ASSET_TRANSPARENT} ({ASSET_TRANSPARENT.stat().st_size} bytes, {transparent.size[0]}x{transparent.size[1]})")

    web = transparent.resize((WEB_WIDTH, round(h * WEB_WIDTH / w)), Image.LANCZOS)
    web.save(PUBLIC_PNG, "PNG", optimize=True)
    print(f"Wrote {PUBLIC_PNG} ({PUBLIC_PNG.stat().st_size} bytes, {web.size[0]}x{web.size[1]})")

    # Application icon / favicon: transparent so it sits on any browser chrome.
    icon = transparent.resize((FAVICON_SIZE, FAVICON_SIZE), Image.LANCZOS)
    icon.save(FAVICON, "PNG", optimize=True)
    print(f"Wrote {FAVICON} ({FAVICON.stat().st_size} bytes, {icon.size[0]}x{icon.size[1]})")


if __name__ == "__main__":
    main()
