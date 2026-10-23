"""Build the BDR Alerts Teams app package (docs/TEAMS_ALERTS.md, 0175).

Writes a zip with manifest.json, color.png (192x192, the G3 logo on white)
and outline.png (32x32, a white bell on transparent). A Teams admin uploads
it once in the Teams admin center (Teams apps > Manage apps > Upload new app);
the server then installs it for each person on their first alert.

The bot id is the Azure Bot's app id: TEAMS_BOT_APP_ID, else MS_CLIENT_ID
(read from the environment / .env like the API). The manifest id is
TEAMS_APP_MANIFEST_ID, the id the sender looks the app up by, so the two
must match on every server that sends alerts.

    uv run python scripts/build_teams_alerts_app.py --out ../bdr-alerts-teams-app.zip

Bump --version when re-uploading a changed package (Teams refuses an
update that keeps the same version).
"""

import argparse
import io
import json
import sys
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

from PIL import Image, ImageDraw

BE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BE_ROOT))

from app.core.config import get_settings  # noqa: E402

LOGO = BE_ROOT / "app" / "assets" / "g3-logo-transparent.png"
NAVY = (32, 33, 89, 255)  # G3 navy #202159


def color_icon() -> bytes:
    canvas = Image.new("RGBA", (192, 192), (255, 255, 255, 255))
    logo = Image.open(LOGO).convert("RGBA")
    logo.thumbnail((150, 150), Image.LANCZOS)
    canvas.alpha_composite(logo, ((192 - logo.width) // 2, (192 - logo.height) // 2))
    buf = io.BytesIO()
    canvas.save(buf, "PNG")
    return buf.getvalue()


def outline_icon() -> bytes:
    """A white bell on transparent, drawn at 4x and scaled down for clean edges."""
    s = 128
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    white = (255, 255, 255, 255)
    d.ellipse((58, 10, 70, 22), fill=white)  # top knob
    d.pieslice((28, 18, 100, 90), 180, 360, fill=white)  # dome
    d.polygon([(28, 54), (100, 54), (110, 96), (18, 96)], fill=white)  # body
    d.rounded_rectangle((12, 92, 116, 102), radius=5, fill=white)  # rim
    d.ellipse((52, 104, 76, 122), fill=white)  # clapper
    buf = io.BytesIO()
    img.resize((32, 32), Image.LANCZOS).save(buf, "PNG")
    return buf.getvalue()


def manifest(bot_id: str, manifest_id: str, version: str, frontend_url: str) -> dict:
    site = frontend_url.rstrip("/")
    host = urlsplit(site).hostname or ""
    return {
        "$schema": "https://developer.microsoft.com/en-us/json-schemas/teams/v1.17/MicrosoftTeams.schema.json",
        "manifestVersion": "1.17",
        "version": version,
        "id": manifest_id,
        "developer": {
            "name": "G3 Electrical",
            "websiteUrl": site,
            "privacyUrl": site,
            "termsOfUseUrl": site,
        },
        "name": {"short": "BDR Alerts", "full": "G3 BDR Alerts"},
        "description": {
            "short": "BDR notifications in Teams.",
            "full": (
                "Sends you the same notifications BDR emails you (assignments, "
                "quotes, deadlines, approvals), each with a button that opens "
                "the page in BDR. Turn it off in BDR under Settings."
            ),
        },
        "icons": {"color": "color.png", "outline": "outline.png"},
        "accentColor": "#202159",
        "bots": [
            {
                "botId": bot_id,
                "scopes": ["personal"],
                "isNotificationOnly": True,
                "supportsFiles": False,
            }
        ],
        "validDomains": [host] if host else [],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--out", default="bdr-alerts-teams-app.zip")
    parser.add_argument("--version", default="1.0.0")
    parser.add_argument(
        "--frontend-url",
        help="The BDR site the manifest names (default: FRONTEND_URL)",
    )
    args = parser.parse_args()

    s = get_settings()
    bot_id = s.teams_bot_app_id or s.ms_client_id
    if not bot_id:
        sys.exit("Set TEAMS_BOT_APP_ID (or MS_CLIENT_ID) first: it is the bot id.")
    frontend_url = args.frontend_url or s.frontend_url
    if not frontend_url.startswith("https://"):
        sys.exit(f"The manifest needs an https site; pass --frontend-url (got {frontend_url}).")

    out = Path(args.out)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(
            "manifest.json",
            json.dumps(manifest(bot_id, s.teams_app_manifest_id, args.version, frontend_url), indent=2),
        )
        z.writestr("color.png", color_icon())
        z.writestr("outline.png", outline_icon())
    print(f"Wrote {out} (bot {bot_id}, manifest id {s.teams_app_manifest_id})")


if __name__ == "__main__":
    main()
