"""MS_DRIVE_OWNER resolution for OneDrive operations.

The send mailbox (MS_SENDER) is a shared mailbox with no OneDrive - drive calls
against it 404 ("User's mysite not found"), which is what broke every oversize
RFQ/submittal send. When MS_DRIVE_OWNER is configured it must win over even an
explicitly passed sender, so uploads, item lookups, and share links all land on
the one provisioned drive.
"""

from app.core.config import Settings
from app.services import graph_email as ge


class _Resp:
    def json(self):
        return {"id": "item-1", "link": {"webUrl": "https://1drv.ms/x"}}


def _capture(monkeypatch, **settings_kw):
    monkeypatch.setattr(ge, "get_settings", lambda: Settings(_env_file=None, **settings_kw))
    seen: dict = {}
    monkeypatch.setattr(
        ge, "graph_request", lambda method, url, **kw: seen.update(url=url) or _Resp()
    )
    return seen


def test_drive_owner_overrides_explicit_sender(monkeypatch):
    seen = _capture(
        monkeypatch, ms_drive_owner="drive.owner@x.com", ms_sender="bids@x.com"
    )
    ge.drive_get_item_id("BDR/p/drawings", sender="ingest@x.com")
    assert "/users/drive.owner@x.com/drive" in seen["url"]

    ge.drive_create_link("item-1", sender="ingest@x.com")
    assert "/users/drive.owner@x.com/drive" in seen["url"]


def test_unset_drive_owner_keeps_current_behavior(monkeypatch):
    seen = _capture(monkeypatch, ms_drive_owner="", ms_sender="bids@x.com")
    ge.drive_get_item_id("BDR/p/drawings", sender="ingest@x.com")
    assert "/users/ingest@x.com/drive" in seen["url"]

    ge.drive_get_item_id("BDR/p/drawings")
    assert "/users/bids@x.com/drive" in seen["url"]
