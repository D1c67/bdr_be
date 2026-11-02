"""The approve and reject routes drop the card's bells through
notifications.dismiss_notifications, which writes only for the types or
type prefixes it is given. Found live on dev (phase 1 live test): the route
passed neither, so the superseded bell stayed after the human acted. This
runs the real dismiss_notifications against a recording client."""

from __future__ import annotations

from app.core.deps import CurrentUser
from app.routers import assistant as router_mod
from app.services import notifications
from app.core.roles import Role

USER = "11111111-1111-4111-8111-111111111111"
APPROVAL = "22222222-2222-4222-8222-222222222222"


class _Query:
    def __init__(self, log: list) -> None:
        self.log = log
        self.calls: list[tuple] = []

    def update(self, values):
        self.calls.append(("update", values))
        return self

    def __getattr__(self, name):
        def method(*args):
            self.calls.append((name, *args))
            return self
        return method

    def execute(self):
        self.log.append(self.calls)
        return self


class _Client:
    def __init__(self) -> None:
        self.executed: list = []

    def table(self, name):
        assert name == "notifications"
        return _Query(self.executed)


def test_dismiss_bells_really_writes_for_the_assistant_bell_types(monkeypatch):
    fake = _Client()
    monkeypatch.setattr(notifications, "get_supabase", lambda: fake)
    user = CurrentUser(id=USER, email="tom@g3.test", role=Role.EXECUTIVE, is_active=True)
    router_mod._dismiss_bells(user, APPROVAL)
    assert len(fake.executed) == 1
    calls = fake.executed[0]
    assert calls[0] == ("update", {"dismissed_at": "now()"})
    assert ("eq", "user_id", USER) in calls
    assert ("eq", "metadata->>approval_id", APPROVAL) in calls
    assert ("like", "type", "assistant.%") in calls
