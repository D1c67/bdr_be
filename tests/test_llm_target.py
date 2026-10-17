"""Live self-hosted box switch (services/llm_target + /model-status/target)."""

from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.services import llm, llm_health, llm_target


def _sh(**over) -> Settings:
    base = dict(
        full_self_hosted_llms_enabled=True,
        self_hosted_llm_target="ec2",
        self_hosted_llm_ec2_base_url="https://llm.example.com/v1",
        self_hosted_llm_ec2_api_key="key-a",
        self_hosted_llm_ec2_model="qwen-3.5-4b",
        self_hosted_llm_ec2b_base_url="https://llm2.example.com/v1",
        self_hosted_llm_ec2b_api_key="key-b",
        self_hosted_llm_ec2b_model="qwen-3.8-27b",
    )
    base.update(over)
    return Settings(_env_file=None, **base)


@pytest.fixture(autouse=True)
def _clean():
    llm_target.reset()
    llm_health.reset_cache()
    yield
    llm_target.reset()
    llm_health.reset_cache()


class _FakeDb:
    """Just enough of the PostgREST builder for llm_target."""

    def __init__(self):
        self.row = {"id": 1, "self_hosted_target": None}
        self.changes: list[dict] = []

    def table(self, name):
        db = self

        class Q:
            def select(self, *_a, **_k):
                self.op = "select"
                return self

            def eq(self, *_a):
                return self

            def limit(self, *_a):
                return self

            def order(self, *_a, **_k):
                return self

            def upsert(self, payload):
                db.row = {**db.row, **payload}
                self.op = "write"
                return self

            def insert(self, payload):
                db.changes.append(payload)
                self.op = "write"
                return self

            def execute(self):
                if getattr(self, "op", "") == "select":
                    data = [db.row] if name == "llm_runtime_settings" else []
                    return SimpleNamespace(data=data)
                return SimpleNamespace(data=[])

        return Q()


def test_no_override_keeps_env_target():
    s = _sh()
    assert llm.resolve("boq", s).base_url == "https://llm.example.com/v1"
    assert llm_target.live_target(s) == "ec2"


def test_override_moves_every_feature_to_the_new_box():
    s = _sh()
    llm_target._set_memory("ec2b")
    route = llm.resolve("boq", s)
    assert route.base_url == "https://llm2.example.com/v1"
    assert route.api_key == "key-b"
    assert route.model == "qwen-3.8-27b"
    assert llm.active_model("proposal", s) == "self-hosted:qwen-3.8-27b"


def test_override_to_unconfigured_box_is_ignored():
    """Box C exists in the list but has no env vars yet: the env default
    keeps serving instead of every feature going dark."""
    s = _sh()
    llm_target._set_memory("ec2c")
    assert llm.resolve("boq", s).base_url == "https://llm.example.com/v1"
    assert "Not set up" in llm_target.target_problem("ec2c", s)


def test_state_lists_box_c_as_not_set_up_and_hides_unconfigured_local(monkeypatch):
    monkeypatch.setattr(llm_target, "_recent_changes", lambda: [])
    monkeypatch.setattr(llm_target, "get_settings", lambda: _sh())
    out = llm_target.state()
    keys = [o["key"] for o in out["options"]]
    assert keys == ["ec2", "ec2b", "ec2c"]
    c = next(o for o in out["options"] if o["key"] == "ec2c")
    assert c["available"] is False and "N. Virginia" in c["detail"]
    assert out["live_target"] == "ec2" and out["default_target"] == "ec2"
    # No URL or key ever leaves the API.
    assert "example.com" not in repr(out) and "key-" not in repr(out)


def test_switch_refuses_unhealthy_box_unless_forced(monkeypatch):
    db = _FakeDb()
    s = _sh()
    monkeypatch.setattr(llm_target, "_db", lambda: db)
    monkeypatch.setattr(llm_target, "get_settings", lambda: s)
    monkeypatch.setattr(
        llm_target, "probe",
        lambda t, _s=None: {"state": "unreachable", "detail": "down", "latency_ms": 1},
    )
    out = llm_target.switch("ec2b", user_id="u1")
    assert out["ok"] is False and db.changes == []
    assert llm_target.current_override() is None

    out = llm_target.switch("ec2b", user_id="u1", force=True)
    assert out["ok"] is True
    assert db.row["self_hosted_target"] == "ec2b"
    assert db.changes[0]["forced"] is True and db.changes[0]["to_target"] == "ec2b"
    assert llm.resolve("boq", s).model == "qwen-3.8-27b"


def test_switch_back_to_default_clears_the_override(monkeypatch):
    db = _FakeDb()
    s = _sh()
    monkeypatch.setattr(llm_target, "_db", lambda: db)
    monkeypatch.setattr(llm_target, "get_settings", lambda: s)
    monkeypatch.setattr(
        llm_target, "probe", lambda t, _s=None: {"state": "ok", "detail": "", "latency_ms": 1}
    )
    llm_target.switch("ec2b", user_id="u1")
    out = llm_target.switch(None, user_id="u1")
    assert out["ok"] and db.row["self_hosted_target"] is None
    assert llm_target.live_target(s) == "ec2"
    assert [c["to_target"] for c in db.changes] == ["ec2b", "ec2"]


def test_switch_to_unconfigured_box_raises(monkeypatch):
    monkeypatch.setattr(llm_target, "get_settings", lambda: _sh())
    with pytest.raises(llm_target.TargetUnavailable):
        llm_target.switch("ec2c", user_id="u1")
    with pytest.raises(llm_target.TargetUnavailable):
        llm_target.switch("nope", user_id="u1")


def test_switch_refuses_when_features_are_pinned_to_another_model(monkeypatch):
    s = _sh(self_hosted_boq_model="qwen-3.5-4b")
    monkeypatch.setattr(llm_target, "get_settings", lambda: s)
    with pytest.raises(llm_target.TargetUnavailable, match="boq"):
        llm_target.switch("ec2b", user_id="u1")


def test_refresh_reads_db_and_keeps_last_value_on_error(monkeypatch):
    db = _FakeDb()
    db.row["self_hosted_target"] = "ec2b"
    monkeypatch.setattr(llm_target, "_db", lambda: db)
    assert llm_target.refresh() == "ec2b"

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(llm_target, "_db", boom)
    assert llm_target.refresh() == "ec2b"


def test_health_label_follows_override():
    s = _sh()
    llm_target._set_memory("ec2b")
    assert "box B" in llm_health._provider_label("self_hosted", llm_target.apply(s))
