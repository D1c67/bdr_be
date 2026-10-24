"""WS5 tools for the tool loop (docs/AI_ESTIMATING_BUILD_SPEC.md 5.2):
`get_scope_policy` and `get_facts`, registered in
`plans_reader_tools.REGISTRY` with strict args.

- `get_scope_policy(topic, item_description)`: the enabled policy rules
  (filtered by topic substring and by an item they fire on), the run's bid
  conditions, standards and code decisions. Read mode; kinds main, review,
  revise, chat. Cites rules as `p:<rule_id>`.
- `get_facts(system)`: the run's F* facts with the verified flag and the
  citation (an unverified fact never auto-excludes). Read mode; kinds main,
  review, chat.

Handlers read through `ctx.svc().policy` (the ScopePolicyService slot; WS2
fakes in tests). When that slot is the Unavailable default and the context
has a Supabase client, they read the stored ScopeContext directly
(`scope_engine.context_for_run`). No pricing data, no model, no writes.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.services import plans_reader_tools as tools
from app.services import scope_engine, scope_policy
from app.services.plans_reader_tools import ToolContext, ToolResult, ToolSpec

MAX_RULES = 40
MAX_FACTS = 60

GET_SCOPE_POLICY_ARGS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "topic": {"type": ["string", "null"], "maxLength": 80,
                  "description": "Topic or rule id substring (temporary_power, fire_alarm, EX-BIM), or null."},
        "item_description": {"type": ["string", "null"], "maxLength": 300,
                             "description": "A BOQ item name; only rules that fire on it are returned. Or null."},
    },
    "required": ["topic", "item_description"],
    "additionalProperties": False,
}
GET_FACTS_ARGS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "system": {"type": ["string", "null"], "maxLength": 40,
                   "description": "A scope system (fire_alarm, access_control, telecom_data, ...) or null for all."},
    },
    "required": ["system"],
    "additionalProperties": False,
}


class RunScopePolicyService:
    """The WS5 ScopePolicyService over a run's stored ScopeContext and the
    active policy. `sb_factory` returns a sync Supabase client."""

    def __init__(self, sb_factory: Callable[[], Any]) -> None:
        self._sb = sb_factory

    def _ctx(self, run_id: str) -> dict[str, Any]:
        try:
            return scope_engine.context_for_run(self._sb(), run_id) or {}
        except Exception:  # noqa: BLE001 - no stored context reads as empty
            return {}

    def rules(self, *, topic: str | None = None, item_description: str | None = None, run_id: str = "") -> dict[str, Any]:
        active = scope_policy.active_policy()
        hits = scope_policy.rules_for(active.doc, topic=topic, item_description=item_description)
        ctx = self._ctx(run_id) if run_id else {}
        bid = ctx.get("bid_conditions") or {}
        cues = [c for c in bid.get("cues") or [] if c.get("weight", 0) > 0]
        return {
            "policy_sha": active.sha256,
            "policy_version": active.version,
            "rules": [
                {
                    "ref": f"p:{r['rule_id']}",
                    "rule_id": r["rule_id"],
                    "topic": r["topic"],
                    "kind": r["kind"],
                    "decision": r["decision"],
                    "default_text": r["default_text"],
                    "proposal_line": r.get("proposal_line"),
                }
                for r in hits[:MAX_RULES]
            ],
            "bid_conditions": {
                "wage_type": bid.get("wage_type"),
                "wage_basis": bid.get("wage_basis"),
                "labor_time": bid.get("labor_time"),
                "basis_refs": [f"{c.get('document_id')}:p{c.get('page')}" for c in cues[:4]],
                "needs_review": list(bid.get("needs_review") or []),
            },
            "standards": {
                k: {"value": v.get("value"), "clarification": v.get("clarification"),
                    "needs_review": v.get("needs_review"), "cite": v.get("cite")}
                for k, v in (ctx.get("standards") or {}).items()
            },
            "decisions": {
                "breaker_plan": (ctx.get("breaker_plan") or {}).get("clarification"),
                "inclusions": [
                    {"rule_id": i.get("rule_id"), "description": i.get("description"), "quantity": i.get("quantity"),
                     "unit": i.get("unit"), "basis": i.get("basis")}
                    for i in ctx.get("inclusions") or []
                ],
                "tr_count": (ctx.get("device_facts") or {}).get("tr_count"),
            },
        }

    def facts(self, *, run_id: str, system: str | None = None) -> dict[str, Any]:
        facts = self._ctx(run_id).get("facts") or []
        if system:
            facts = [f for f in facts if f.get("system") == system]
        return {"facts": facts[:MAX_FACTS], "total": len(facts)}


def _service(ctx: ToolContext) -> Any:
    svc = ctx.svc().policy
    if isinstance(svc, tools.Unavailable) and ctx.sb is not None:
        sb = ctx.sb
        return RunScopePolicyService(lambda: sb)
    return svc


def handle_get_scope_policy(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    svc = _service(ctx)
    data = svc.rules(topic=args.get("topic"), item_description=args.get("item_description"), run_id=ctx.run_id)
    if tools.is_unavailable(data):
        return ToolResult.refusal("unavailable", data.get("message") or "the scope policy is unavailable", data)
    refs = [r.get("ref") for r in data.get("rules") or [] if r.get("ref")]
    return ToolResult.ok(data, citations=refs)


def handle_get_facts(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    svc = _service(ctx)
    fn = getattr(svc, "facts", None)
    if fn is None:
        return ToolResult.refusal("unavailable", "the facts service is not available on this server yet")
    data = fn(run_id=ctx.run_id, system=args.get("system"))
    if tools.is_unavailable(data):
        return ToolResult.refusal("unavailable", data.get("message") or "facts are unavailable", data)
    out = []
    for f in data.get("facts") or []:
        cite = f.get("cite") or {}
        out.append({
            "id": f.get("id"),
            "system": f.get("system"),
            "party": f.get("party"),
            "verified": bool(f.get("verified")),
            "kind": f.get("kind"),
            "statement": str(f.get("statement") or "")[:300],
            "cite": cite.get("section"),
            "page": cite.get("page"),
        })
    refs = [f["id"] for f in out if f.get("id")]
    return ToolResult.ok({"facts": out, "total": data.get("total", len(out)),
                          "note": "An unverified fact never excludes scope; cite fact ids in exclusions."},
                         citations=refs)


GET_SCOPE_POLICY = ToolSpec(
    name="get_scope_policy",
    description=(
        "The G3 scope policy for this run: rules (cite as p:<rule_id>), bid conditions (wage type), "
        "standards (raceway, demolition disposition) and code decisions (breakers, inclusions). Binding."
    ),
    args_schema=GET_SCOPE_POLICY_ARGS,
    result_doc={"rules": "list", "bid_conditions": "object", "standards": "object", "decisions": "object"},
    mode="read",
    kinds=tools.kinds_from_codes("M, RV, RS, C"),
    handler=handle_get_scope_policy,
    max_calls_per_group=12,
)
GET_FACTS = ToolSpec(
    name="get_facts",
    description=(
        "Scope facts F* found in the documents (who carries a system: owner vendor, other trade, by others, "
        "general contractor, not used), each with a verified flag and its citation."
    ),
    args_schema=GET_FACTS_ARGS,
    result_doc={"facts": "list", "total": "int"},
    mode="read",
    kinds=tools.kinds_from_codes("M, RV, C"),
    handler=handle_get_facts,
    max_calls_per_group=12,
)


def register() -> None:
    """Register both tools (idempotent: a second call is a no-op)."""
    for spec in (GET_SCOPE_POLICY, GET_FACTS):
        if tools.get_spec(spec.name) is None:
            tools.register(spec)


register()

__all__ = ["GET_SCOPE_POLICY", "GET_FACTS", "RunScopePolicyService", "handle_get_scope_policy", "handle_get_facts",
           "register"]
