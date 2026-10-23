"""The BDR Alerts bot's messaging endpoint (docs/TEAMS_ALERTS.md, 0175).

An Azure Bot needs an https messaging endpoint, and Teams posts the bot's
install and uninstall events there. BDR Alerts is notification only (the
manifest sets isNotificationOnly, so nobody can message it) and sends every
alert proactively through the Bot Connector, so this endpoint has nothing to
do: it reads nothing from the request and answers 200 so Teams does not
retry. Because it acts on nothing, it needs no Bot Framework token check.
"""

from fastapi import APIRouter, Response

router = APIRouter(prefix="/teams", tags=["teams"])


@router.post("/messages", include_in_schema=False)
def bot_messages() -> Response:
    return Response(status_code=200)
