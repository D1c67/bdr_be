"""The chat's inbox read tool `inbox_list` (docs/ASSISTANT_PHASE8_PLAN.md 6.1,
6.3, 15.2; owner question 16 (a); WI-10).

Invariants pinned here:

  spec       one deferred read tool in the new `inbox` namespace (not an own
             row namespace), every internal role, no confirm, standard
             retention; its route literal equals the router's path; the
             schema is strict with every key required and nullable where
             optional;
  requests   one GET of the tool route; null arguments are dropped, an
             unknown segment falls back to unread;
  trimmer    the documented keys only, ids shortened through the executor's
             short ids, text capped, digests only for the digests segment;
  taint      any non-empty summary or detail taints the turn (the keys are
             not whitelist keys), an empty page does not;
  route      end to end over the real route and fakes: hidden and dismissed
             rows are excluded, nothing is marked read or dismissed, the
             tool is deferred and found by find_tools, and the follow-up
             and Mailbox review toolsets keep it as plan 6.1 says.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.assistant import executor as executor_mod
from app.assistant import find, proactive, registry
from app.assistant.tools import inbox as inbox_tool
from app.core.roles import INTERNAL_ROLES, Role
from app.routers import assistant_inbox_tools as tools_router

from tests.assistant_fakes import BELL, BELL2, INBOX2, USER, bell_row
from tests import test_assistant_triage_routes as _routes
from tests.test_assistant_triage_routes import _as_assistant, _put, _record_row, _router_row, _seed_tool_rows

# The route tests' fake-backed app fixture, reused by name.
api = _routes.api

ROOT = Path(__file__).resolve().parents[1]
EM_DASH = chr(0x2014)
EN_DASH = chr(0x2013)
FLAGS = {"bidding": True, "pm": True, "certified_payroll": True, "ai_estimating": True}
ROW_KEYS = {"id", "kind", "kind_label", "group", "severity", "delivery", "from_bell", "project_number",
            "project_id", "received_at", "read", "triage", "summary", "detail"}


def _spec():
    return registry.get("inbox_list")


def _user(role):
    return SimpleNamespace(id="u", role=role, is_active=True)


def _ctx(fmt="concise", limit=20, role=Role.EXECUTIVE):
    ids: dict[str, str] = {}

    def short(value):
        full = str(value or "")
        ids[full[:8]] = full
        return full[:8]

    return registry.TrimContext(response_format=fmt, limit=limit, short=short, role=role), ids


def _route_row(**over) -> dict:
    base = {
        "id": "11111111-1111-4111-8111-111111111111", "kind": "bell.late_quote", "kind_label": "Late quote",
        "group": "Bidding", "severity": "low", "delivery": "record", "from_bell": True,
        "project_number": "26.10.7301", "project_id": "22222222-2222-4222-8222-222222222222",
        "received_at": "2026-10-30 7:00 AM PT", "read": False,
        "triage": {"decision": "suggest", "reason": "info_only", "action": "open_receive_quotes"},
        "summary": "A late quote arrived on bid 26.10.7301.", "detail": "BDR already told you.",
    }
    base.update(over)
    return base


# -- The spec ------------------------------------------------------------------


def test_the_constants_equal_the_router_route_and_the_spec():
    assert inbox_tool.ROUTE_PATH == tools_router.ROUTE_PATH == "/assistant/tools/inbox"
    spec = _spec()
    assert spec.route == ("GET", inbox_tool.ROUTE_PATH) and spec.routes == (spec.route,)
    assert spec.name == inbox_tool.TOOL_NAME == "inbox_list"
    assert spec.namespace == inbox_tool.NAMESPACE == "inbox"
    assert spec.progress_label == inbox_tool.PROGRESS_LABEL == "Reading your inbox"
    assert spec.keywords == inbox_tool.KEYWORDS and spec.taint_fields == inbox_tool.TAINT_FIELDS
    assert inbox_tool.SEGMENTS == ("unread", "all", "digests")
    live = {(m, r.path) for r in tools_router.router.routes for m in r.methods}
    assert spec.route in live


def test_it_is_one_deferred_read_in_a_new_non_own_row_namespace():
    spec = _spec()
    assert spec.effect == registry.EFFECT_READ and spec.confirm is False and spec.write is None
    assert spec.deferred is True and spec.offered is True and spec.sub_app is None
    assert spec.roles == INTERNAL_ROLES and spec.retention_class == "standard"
    assert "inbox" in registry.NAMESPACES and "inbox" not in registry.OWN_ROW_NAMESPACES
    assert "inbox" not in registry.NEVER_DEFERRED_NAMESPACES
    assert [n for n, s in registry.all_tools().items() if s.namespace == "inbox"] == ["inbox_list"]
    assert spec.redact_args is None and spec.precheck is None and spec.max_proposals_per_turn is None


def test_the_schema_is_strict_with_every_key_required():
    props = _spec().schema["properties"]
    assert set(props) == {"segment", "kind", "project", "before", "limit", "response_format"}
    assert set(_spec().schema["required"]) == set(props) and _spec().schema["additionalProperties"] is False
    assert props["segment"]["enum"] == ["unread", "all", "digests"] and props["segment"]["type"] == "string"
    for key in ("kind", "project", "before", "limit"):
        assert "null" in props[key]["type"], key
    assert "null" not in props["segment"]["type"]
    kinds = set(props["kind"]["enum"])
    assert {proactive.TRIAGE_ESCALATION, "bell.quote_received", "assistant.calendar"} <= kinds
    assert kinds >= set(proactive.EVENT_TYPES)
    # No free-text argument, so nothing to redact in the ledger.
    assert not any(p.get("type") == "string" and "enum" not in p and k not in ("project", "before")
                   for k, p in props.items() if k != "kind")


def test_definition_has_no_dash_and_is_stable():
    text = json.dumps(_spec().definition())
    assert EM_DASH not in text and EN_DASH not in text
    assert _spec().definition() == _spec().definition()


# -- Who is offered it, and how it is found ------------------------------------


@pytest.mark.parametrize("role", sorted(INTERNAL_ROLES, key=lambda r: r.value))
def test_every_internal_role_is_offered_it_whatever_the_flags(role):
    for flags in (FLAGS, {k: False for k in FLAGS}):
        assert "inbox_list" in {s.name for s in registry.toolset_for(_user(role), flags)}


def test_an_external_estimator_never_gets_it():
    assert registry.toolset_for(_user(Role.ESTIMATOR), FLAGS) == []


def test_mailbox_review_and_the_follow_up_toolset_keep_it_as_plan_6_1_says():
    names = lambda **kw: {s.name for s in registry.toolset_for(_user(Role.EXECUTIVE), FLAGS, **kw)}  # noqa: E731
    assert "inbox_list" in names(mode=registry.MODE_MAILBOX_REVIEW)
    assert "inbox_list" in names(followup=True, browser=False)
    assert "inbox_list" in names(incognito=True)
    accountant = {s.name for s in registry.toolset_for(_user(Role.ACCOUNTANT), FLAGS)}
    assert "inbox_list" in accountant


def test_it_is_deferred_so_only_the_catalog_line_names_it():
    specs = registry.toolset_for(_user(Role.EXECUTIVE), FLAGS, browser=False)
    assert find.area_for(_spec()) == find.AREA_INBOX
    assert find.AREA_INBOX in find.catalog_line([_spec()])
    assert EM_DASH not in find.AREA_INBOX and EN_DASH not in find.AREA_INBOX
    for query in ("inbox", "what did my assistant flag", "digest", "new notifications", "alerts", "updates"):
        assert "inbox_list" in [s.name for s in find.resolve(query, specs, limit=6)], query
    # A deferred tool's definition is not in the always-offered set.
    assert _spec().deferred and "inbox_list" not in {s.name for s in specs if not s.deferred}


# -- The request builder -------------------------------------------------------


def test_the_request_is_one_get_of_the_tool_route():
    (req,) = _spec().requests({
        "segment": "all", "kind": "bell.verify", "project": " 26.10.7301 ",
        "before": "2026-11-02T10:00:00.000000Z|55555555-5555-4555-8555-555555555555",
        "limit": 5, "response_format": "concise",
    })
    assert req.route == _spec().route and req.path == "/assistant/tools/inbox" and req.json is None
    assert req.params == {
        "segment": "all", "limit": 5, "kind": "bell.verify", "project": "26.10.7301",
        "before": "2026-11-02T10:00:00.000000Z|55555555-5555-4555-8555-555555555555",
    }
    assert req.route in _spec().routes


def test_null_arguments_are_dropped_and_an_unknown_segment_is_unread():
    (req,) = _spec().requests({"segment": "weird", "kind": None, "project": "  ", "before": None, "limit": None})
    assert req.params == {"segment": "unread", "limit": registry.DEFAULT_LIMIT}
    (req,) = _spec().requests({"limit": 999})
    assert req.params["limit"] == registry.MAX_LIMIT


# -- The trimmer ---------------------------------------------------------------


def test_the_trimmer_keeps_the_documented_keys_and_shortens_ids():
    ctx, ids = _ctx()
    out = _spec().response_trimmer([{"items": [_route_row()], "next": "cursor-1"}], {"segment": "all"}, ctx)
    assert set(out) == {"items", "next_cursor"} and out["next_cursor"] == "cursor-1"
    (row,) = out["items"]
    assert set(row) == ROW_KEYS
    assert row["id"] == "11111111" and row["project_id"] == "22222222"
    assert ids["11111111"] == "11111111-1111-4111-8111-111111111111"
    assert row["triage"] == {"decision": "suggest", "reason": "info_only", "action": "open_receive_quotes"}
    assert row["read"] is False and row["from_bell"] is True and row["project_number"] == "26.10.7301"
    # Never `title` or `why`: those are taint whitelist keys.
    assert "title" not in row and "why" not in row
    assert not ({"summary", "detail"} & executor_mod.TAINT_WHITELIST_KEYS)


def test_the_trimmer_drops_nothing_that_is_false_and_nulls_what_is_absent():
    ctx, _ids = _ctx()
    out = _spec().response_trimmer([{"items": [_route_row(triage=None, project_id=None, project_number=None,
                                                          from_bell=False, read=True)], "next": None}],
                                    {"segment": "all"}, ctx)
    (row,) = out["items"]
    assert row["read"] is True and row["from_bell"] is False
    assert "triage" not in row and "project_id" not in row and "project_number" not in row
    assert "next_cursor" not in out and "digests" not in out


def test_the_trimmer_caps_text_and_the_page_by_limit():
    long = "word " * 400
    ctx, _ids = _ctx(limit=2)
    rows = [_route_row(id=f"{i}1111111-1111-4111-8111-111111111111", summary=long, detail=long) for i in range(4)]
    out = _spec().response_trimmer([{"items": rows, "next": None}], {"segment": "all"}, ctx)
    assert len(out["items"]) == 2 and "2 of 4 shown" in out["hint"]
    assert len(out["items"][0]["summary"]) <= 240
    wide, _ids = _ctx(fmt="detailed", limit=2)
    out = _spec().response_trimmer([{"items": rows, "next": None}], {"segment": "all"}, wide)
    assert 240 < len(out["items"][0]["summary"]) <= 1200


def test_digests_come_back_only_for_the_digests_segment():
    ctx, _ids = _ctx()
    body = {"items": [], "next": None, "digests": [{
        "id": "33333333-3333-4333-8333-333333333333", "slot": "am", "date": "2026-10-30",
        "item_count": 3, "record_count": 2}]}
    out = _spec().response_trimmer([body], {"segment": "digests"}, ctx)
    assert out["digests"] == [{"id": "33333333", "slot": "am", "date": "2026-10-30", "item_count": 3,
                               "record_count": 2}]
    assert out["items"] == []
    assert "digests" not in _spec().response_trimmer([body], {"segment": "all"}, ctx)


def test_the_trimmer_survives_a_missing_or_odd_payload():
    ctx, _ids = _ctx()
    assert _spec().response_trimmer([], {}, ctx) == {"items": []}
    assert _spec().response_trimmer([None], {}, ctx) == {"items": []}
    assert _spec().response_trimmer([{"items": ["x", None, 3]}], {}, ctx) == {"items": []}


# -- Taint ---------------------------------------------------------------------


def _shape(payload, args=None):
    ex = executor_mod.Executor(user_id=USER, user_label="Tom", run_id="r", specs={"inbox_list": _spec()},
                               lease_owner="w", conversation_id="c", message_id="m")
    return ex.shape(_spec(), [payload], args or {"segment": "all", "limit": None, "response_format": "concise"})


def test_any_non_empty_row_taints_the_turn():
    output, tainted = _shape({"items": [_route_row()], "next": None})
    assert tainted is True and output["items"][0]["summary"]
    # Either field alone is enough.
    assert _shape({"items": [_route_row(detail=None)], "next": None})[1] is True
    assert _shape({"items": [_route_row(summary=None)], "next": None})[1] is True


def test_an_empty_page_or_a_row_without_text_does_not_taint():
    assert _shape({"items": [], "next": None})[1] is False
    assert _shape({"items": [_route_row(summary=None, detail="  ")], "next": None})[1] is False


def test_the_digests_segment_alone_is_not_text():
    body = {"items": [], "next": None, "digests": [{
        "id": "33333333-3333-4333-8333-333333333333", "slot": "pm", "date": "2026-10-30",
        "item_count": 1, "record_count": 0}]}
    assert _shape(body, {"segment": "digests", "limit": None, "response_format": "concise"})[1] is False


# -- End to end over the real route and fakes -----------------------------------


def _through_the_tool(api, **params):
    """The route's JSON through the tool's request builder and trimmer."""
    args = {"segment": "all", "kind": None, "project": None, "before": None, "limit": None,
            "response_format": "concise", **params}
    (req,) = _spec().requests(args)
    res = api.client.get(req.path, params=req.params)
    assert res.status_code == 200, res.text
    ctx, _ids = _ctx()
    return _spec().response_trimmer([res.json()], args, ctx)


def test_the_real_route_feeds_the_trimmer_and_taints(api):
    _seed_tool_rows(api)
    _as_assistant(api)
    out = _through_the_tool(api)
    assert [r["kind"] for r in out["items"]] == ["bell.late_quote", "todo.due", "assistant.calendar"]
    assert all(set(r) <= ROW_KEYS for r in out["items"])
    assert out["items"][0]["triage"]["decision"] == "escalate"
    assert _shape(api.client.get("/assistant/tools/inbox", params={"segment": "all"}).json())[1] is True


def test_dismissed_and_hidden_rows_are_excluded_and_nothing_is_written(api):
    api.db.tables["notifications"] += [
        bell_row(id=BELL2),
        bell_row(id=BELL, dismissed_at="2026-10-30T07:00:00+00:00"),
    ]
    _put(api, _record_row())                                                          # visible
    _put(api, _record_row(id="b3b3b3b3-b3b3-4b3b-8b3b-b3b3b3b3b3b3", dedupe_key="p7:bell:b3",
                          source_notification_id=BELL))                               # source bell dismissed
    _put(api, _router_row(id="b4b4b4b4-b4b4-4b4b-8b4b-b4b4b4b4b4b4", dedupe_key="x4", notification_id=None,
                          dismissed_at="2026-10-30T07:00:00+00:00", dismissed_reason="user"))
    _as_assistant(api)
    before = {k: [dict(r) for r in v] for k, v in api.db.tables.items()}
    calls = len(api.db.calls)
    out = _through_the_tool(api)
    assert [r["id"] for r in out["items"]] == [INBOX2[:8]]
    _through_the_tool(api, segment="unread")
    assert api.db.tables == before
    assert {op for op, _t in api.db.calls[calls:]} == {"select"}
    assert all(not n.startswith(("assistant_inbox_mark", "assistant_inbox_dismiss", "assistant_inbox_feedback"))
               for n, _ in api.db.rpc_calls)


def test_the_project_number_filters_through_the_tool(api):
    _seed_tool_rows(api)
    _as_assistant(api)
    out = _through_the_tool(api, project="26.10.7302")
    assert [r["project_number"] for r in out["items"]] == ["26.10.7302"]
    out = _through_the_tool(api, kind="bell.late_quote")
    assert [r["kind"] for r in out["items"]] == ["bell.late_quote"]


def test_a_short_id_in_project_answers_unknown_project_and_the_number_path_works(api):
    """The model only ever sees short ids, and the executor does not expand the
    `project` key, so a short id is a coded 422. The schema names only the
    project number, which is the form that works."""
    _seed_tool_rows(api)
    _as_assistant(api)
    desc = _spec().schema["properties"]["project"]["description"]
    assert "project number" in desc and "full project id" not in desc and "26.10.7301" in desc
    (req,) = _spec().requests({"segment": "all", "kind": None, "project": "22222222", "before": None,
                               "limit": None, "response_format": "concise"})
    res = api.client.get(req.path, params=req.params)
    assert res.status_code == 422 and "unknown_project" in res.text
    assert "Use the project number." in res.text and "full project id" not in res.text
    out = _through_the_tool(api, project="26.10.7302")
    assert [r["project_number"] for r in out["items"]] == ["26.10.7302"]


# -- Source hygiene --------------------------------------------------------------


def test_the_tool_module_is_pure_and_dash_free():
    text = (ROOT / "app/assistant/tools/inbox.py").read_text(encoding="utf-8")
    assert EM_DASH not in text and EN_DASH not in text
    for banned in ("get_supabase", "supabase_client", ".table(", "app.services", "app.routers", "app.assistant.repo"):
        assert banned not in text, banned
    assert _spec().description.count("untrusted") == 1
