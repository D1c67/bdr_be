"""The tool registry (docs/ASSISTANT.md, Skills and tools).

A tool is a declared mapping to one or more real HTTP routes of the served
app, executed in-process through the shim under the bdra_ principal. The
registry holds every ToolSpec, refuses a spec without a route, builds the
per-user toolset from the sub-app flags, the role, the kill switches and the
conversation mode, and serializes the tool definitions for the model in a
byte-stable order (sorted names, sorted keys) so OpenAI prefix caching sees
the same bytes for the same (flags, role) pair.

Phase 1 (docs/ASSISTANT_PHASE1_PLAN.md 3.2): a write_internal spec carries
a WriteSpec. Inside a turn only its preview routes are reachable
(`turn_allowlist`); the write route runs only in the approval execution
(`execute_allowlist`). Hidden specs (`offered=False`) are registry-known for
reverts but never offered to the model.

Phase 3 (docs/ASSISTANT_PHASE3_PLAN.md 3.7.1) adds the housekeeping tier:
inline tools that write only the user's own assistant rows (memory and
schedule bookkeeping). They run in the turn like a read, under a ledger
row, never as a card. A spec may carry `redact_args` (the form its
arguments take in the ledger) and `precheck` (a refusal decided before any
ledger row or request).

Phase 4 (docs/ASSISTANT_PHASE4_PLAN.md 3.1, 3.2) makes sensitive_read a
working tier: a sensitive spec carries a WriteSpec whose one "write" is an
approval-bound POST under /analytics/evidence/ that logs the access and
returns the rows, gated to EVIDENCE_ROLES, with a `reason` argument used
only as the card's prefill. Specs gain `retention_class` (payroll results
expire after 7 days), `deferred` (definitions reach the model only after
find_tools or a skill surfaces them) and `keywords` (the find resolver).
`per_person_violation` refuses a non-sensitive request that names one
person on a per-person route.

Nothing here reads the database: `toolset_for` takes the rows it needs.
The tool modules under app/assistant/tools register themselves on import;
`all_tools()` imports them lazily so this module stays import-light.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.core.roles import EVIDENCE_ROLES, INTERNAL_ROLES, Role

RoutePair = tuple[str, str]

# POST routes that only read (phase 1 previews): a write spec may list them
# in extra_routes next to GET routes.
READ_POST_ROUTES: frozenset[RoutePair] = frozenset({("POST", "/projects/similar"), ("POST", "/assistant/mail/authority"), ("POST", "/assistant/browser-tasks/preview")})

EFFECT_READ = "read"
EFFECT_SENSITIVE_READ = "sensitive_read"
EFFECT_WRITE_INTERNAL = "write_internal"
EFFECT_EXTERNAL_SEND = "external_send"
EFFECT_EXPENSIVE = "expensive"
# Phase 3: inline writes to the user's own assistant rows, ledgered, no card.
EFFECT_HOUSEKEEPING = "housekeeping"
EFFECTS: frozenset[str] = frozenset({
    EFFECT_READ, EFFECT_SENSITIVE_READ, EFFECT_WRITE_INTERNAL, EFFECT_EXTERNAL_SEND,
    EFFECT_EXPENSIVE, EFFECT_HOUSEKEEPING,
})
# The tiers a Mailbox review conversation may hold (Prompt injection and taint).
READ_EFFECTS: frozenset[str] = frozenset({EFFECT_READ, EFFECT_SENSITIVE_READ})
# Effects that never run inside the turn: they write an approval card.
CONFIRM_EFFECTS: frozenset[str] = frozenset({
    EFFECT_SENSITIVE_READ, EFFECT_WRITE_INTERNAL, EFFECT_EXTERNAL_SEND, EFFECT_EXPENSIVE,
})

# Effects whose spec must carry a WriteSpec (phase 2, 3.9.1; phase 4 adds
# sensitive_read, whose "write" is the approval-bound evidence POST).
WRITE_SPEC_EFFECTS: frozenset[str] = frozenset({
    EFFECT_WRITE_INTERNAL, EFFECT_EXPENSIVE, EFFECT_EXTERNAL_SEND, EFFECT_SENSITIVE_READ,
})

ALLOWED_METHODS: frozenset[str] = frozenset({"GET", "POST", "PUT", "PATCH"})
SUB_APPS: frozenset[str] = frozenset({"bidding", "pm", "certified_payroll", "ai_estimating"})
NAMESPACES: frozenset[str] = frozenset({
    "core", "bidding", "pm", "payroll", "memory", "schedule", "plans", "evidence", "report", "mail", "web", "browser",
})
# The namespaces whose tools change only the user's own assistant rows. A
# housekeeping tool lives in one of them; Mailbox review drops them and
# Incognito drops memory (docs/ASSISTANT.md, Memory). Phase 4 adds report
# (build_report: the workbook is the user's own private artifact, D5).
OWN_ROW_NAMESPACES: frozenset[str] = frozenset({"memory", "schedule", "report"})
HOUSEKEEPING_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH"})

# Phase 4 (3.1). Tool result retention classes a spec may name (the 0170
# column also allows cross_mailbox, which the mailbox phase sets itself).
RETENTION_STANDARD = "standard"
RETENTION_PAYROLL_AGGREGATE = "payroll_aggregate"
RETENTION_MAIL = "mail"
RETENTION_CROSS_MAILBOX = "cross_mailbox"
RETENTION_CLASSES: frozenset[str] = frozenset({RETENTION_STANDARD, RETENTION_PAYROLL_AGGREGATE, RETENTION_MAIL, RETENTION_CROSS_MAILBOX})
# Namespaces whose tools are always offered (never deferred).
NEVER_DEFERRED_NAMESPACES: frozenset[str] = frozenset({"core", "memory", "schedule"})
# The sensitive_read write route lives under this prefix (3.2, 3.8.6).
EVIDENCE_PREFIX = "/analytics/evidence/"
# The argument a sensitive spec proposes its reason in (the card prefill).
REASON_ARG = "reason"
REASON_MIN_CHARS, REASON_MAX_CHARS = 10, 500
# Per-person routes (3.1): only a sensitive_read spec may name one person
# on them, by any of these keys in the query, the JSON body or the path.
PERSON_KEYS: frozenset[str] = frozenset({"actor_id", "user_id", "file_id"})
PER_PERSON_PATHS: frozenset[str] = frozenset({
    "/analytics/file-access", "/analytics/activity", "/projects/{project_id}/notification-log",
})
_KEYWORD_RE = re.compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$")

MODE_NORMAL = "normal"
MODE_MAILBOX_REVIEW = "mailbox_review"

# Arguments every tool accepts (Skills and tools: response_format and limit).
RESPONSE_FORMATS = ("concise", "detailed")
DEFAULT_LIMIT = 20
MAX_LIMIT = 50

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,60}$")
_PATH_FORMAT_RE = re.compile(r"^/[A-Za-z0-9_{}./-]*$")


class RegistryError(ValueError):
    """A ToolSpec the registry refuses (no route, bad shape, duplicate)."""


class ProposalRefused(ValueError):
    """The tool cannot propose this change (nothing to change, project
    abandoned, GC already on the project...). The message goes to the model."""


class RevertNotPossible(ValueError):
    """revert_args cannot express the inverse (for example the bidding
    question was unanswered before). The message is shown on the row."""


@dataclass(frozen=True)
class ToolRequest:
    """One HTTP request a tool makes through the shim. `path` is concrete
    (ids substituted); `route` is the (METHOD, path_format) it must match,
    which the firewall also checks. `optional` requests may 404 (a sub-app
    that is off) without failing the tool."""

    route: RoutePair
    path: str
    params: Mapping[str, Any] = field(default_factory=dict)
    json: Any = None
    optional: bool = False
    # Extra request headers (an idempotency key, a compare-and-set value).
    # Authorization and Accept are always the executor's own.
    headers: Mapping[str, str] = field(default_factory=dict)

    @property
    def method(self) -> str:
        return self.route[0]


@dataclass(frozen=True)
class TrimContext:
    """What a response_trimmer may use: the requested format and limit, and
    `short`, which registers a full id with the executor and returns the
    short form the model sees (names plus short ids)."""

    response_format: str
    limit: int
    short: Callable[[Any], str]
    role: Role | None = None


Trimmer = Callable[[list[Any], Mapping[str, Any], TrimContext], Any]
RequestBuilder = Callable[[Mapping[str, Any]], list[ToolRequest]]


@dataclass(frozen=True)
class BuildContext:
    """What a write spec's `build` may use besides the args and the reads."""

    approval_id: str              # pre-generated uuid of the row being built
    role: Role | None
    short: Callable[[Any], str]
    tainted: bool
    stripped: tuple[dict, ...]    # [{"field", "reason"}] from binding
    now: datetime                 # UTC
    is_revert: bool = False       # True only when propose() was called with reverts_approval_id


@dataclass(frozen=True)
class Proposal:
    """What `build` returns: the one write and the card it becomes."""

    request: ToolRequest          # the one write; request.route == spec.route
    project_id: str | None
    target: dict                  # canonical-JSON-safe
    gate: dict                    # canonical-JSON-safe
    preview: dict                 # docs/ASSISTANT_PHASE1_PLAN.md 3.6
    summary: str                  # one line for the model, no em dash


@dataclass(frozen=True)
class WriteSpec:
    """The confirm half of a write_internal tool (phase 1). A proposal reads
    the target through the preview routes, `build` turns the args and the
    reads into one write plus a card, and the execute job re-reads through
    `snapshot` to check the precondition before it sends the write."""

    preview_requests: Callable[[Mapping[str, Any]], list[ToolRequest]]
    build: Callable[[Mapping[str, Any], list[Any], BuildContext], Proposal]
    snapshot: Callable[[Mapping[str, Any], list[Any]], tuple[dict, dict]]   # (target, gate) from reads
    bound_text_fields: tuple[str, ...] = ()   # dotted paths into args, e.g. "changes.notes"
    bound_url_fields: tuple[str, ...] = ()
    # (args, preview reads) -> {bound text path: texts already stored in the
    # record the write targets}. Binding keeps a value equal to one of them
    # (BDR data resent unchanged, for example the existing labor breakdown
    # line names a whole-list replace must carry), never foreign text.
    bind_keep: Callable[[Mapping[str, Any], list[Any]], Mapping[str, Iterable[str]]] | None = None
    revert_tool: str | None = None
    # (orig args, before snapshot {"target","gate"}, after snapshot
    # {"target", ...}) -> revert args; raises RevertNotPossible.
    revert_args: Callable[[Mapping[str, Any], dict, dict], dict] | None = None
    creates: str | None = None                # "project" | "project_gc" | "todo"
    # (args, write response) -> {"kind","id","label","href"}
    created_ref: Callable[[Mapping[str, Any], Any], dict | None] | None = None
    result_summary: Callable[[Any], dict] | None = None
    # (args, execute-time reads) -> extra headers, not hashed
    exec_headers: Callable[[Mapping[str, Any], list[Any]], dict] | None = None
    # (args, approval_id) -> GET that finds a created row after a 5xx
    recover_request: Callable[[Mapping[str, Any], str], ToolRequest] | None = None
    writes_tables: tuple[str, ...] = ()       # every table the write route touches, checked against 0172


@dataclass(frozen=True)
class ToolSpec:
    name: str
    namespace: str
    sub_app: str | None
    route: RoutePair                      # the primary (METHOD, path_format)
    effect: str
    confirm: bool
    schema: dict                          # JSON schema of the arguments, strict
    response_trimmer: Trimmer
    progress_label: str
    roles: frozenset[Role]
    description: str
    requests: RequestBuilder
    # Further routes the tool calls after the primary one (all allowlisted).
    extra_routes: tuple[RoutePair, ...] = ()
    # Output keys whose text is authored by humans or by ingestion; a result
    # that carries any of them non-empty taints the turn (Prompt injection
    # and taint). Matched at any depth of the trimmed output.
    taint_fields: tuple[str, ...] = ()
    version: str = "1"
    # Phase 1: the confirm half of a write_internal tool.
    write: WriteSpec | None = None
    # False: registry-known (revert, execute) but never offered to the model.
    offered: bool = True
    # Phase 2 (docs/ASSISTANT_PHASE2_PLAN.md 3.9.4): per-turn ceilings the
    # loop enforces. max_cards_per_turn bounds the cards of this tool in one
    # answer; max_proposals_per_turn bounds the attempts (refused or not),
    # checked before the preview is read. None means no tool-specific limit.
    max_cards_per_turn: int | None = None
    max_proposals_per_turn: int | None = None
    # Phase 3 (docs/ASSISTANT_PHASE3_PLAN.md 3.7.1). redact_args: the form
    # the arguments take in every ledger row (None: the 8 KB cap only).
    # precheck: (args) -> (error_kind, message) refuses the call before any
    # ledger row or request, else None. max_tainted_calls_per_turn: the
    # calls a housekeeping tool may make while the turn is tainted.
    redact_args: Callable[[Mapping[str, Any]], dict] | None = None
    precheck: Callable[[Mapping[str, Any]], tuple[str, str] | None] | None = None
    max_tainted_calls_per_turn: int | None = None
    # Phase 4 (docs/ASSISTANT_PHASE4_PLAN.md 3.1). retention_class: the
    # stored result's class ("payroll_aggregate" expires after 7 days, 3.14).
    # deferred: the definition reaches the model only once find_tools or a
    # loaded skill surfaces it (3.16). keywords: lowercase words the find
    # resolver scores against.
    retention_class: str = RETENTION_STANDARD
    deferred: bool = False
    keywords: tuple[str, ...] = ()

    @property
    def routes(self) -> tuple[RoutePair, ...]:
        return (self.route,) + tuple(self.extra_routes)

    def definition(self) -> dict:
        """The OpenAI function tool definition, keys sorted."""
        return _sorted({
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": self.schema,
            "strict": True,
        })


_REGISTRY: dict[str, ToolSpec] = {}
_LOADED = False


def _sorted(value: Any) -> Any:
    """A deep copy with every dict's keys sorted (lists keep their order)."""
    if isinstance(value, dict):
        return {k: _sorted(value[k]) for k in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_sorted(v) for v in value]
    return value


def normalize_route(route: Any) -> RoutePair:
    try:
        method, path_format = route
    except (TypeError, ValueError):
        raise RegistryError("a ToolSpec route is a (METHOD, path_format) pair") from None
    method = str(method or "").upper()
    path_format = str(path_format or "")
    if method not in ALLOWED_METHODS:
        raise RegistryError(f"route method {method!r} is not allowed (DELETE is refused in the shim)")
    if not _PATH_FORMAT_RE.match(path_format) or path_format == "/":
        raise RegistryError(f"route path_format {path_format!r} is not a route template")
    return (method, path_format)


def validate(spec: ToolSpec) -> ToolSpec:
    """Refuse a spec that could not run safely. Returns the spec with its
    routes normalized."""
    if not _NAME_RE.match(spec.name or ""):
        raise RegistryError(f"tool name {spec.name!r} must be snake_case, 3 to 61 characters")
    if spec.namespace not in NAMESPACES:
        raise RegistryError(f"tool {spec.name}: unknown namespace {spec.namespace!r}")
    if spec.sub_app is not None and spec.sub_app not in SUB_APPS:
        raise RegistryError(f"tool {spec.name}: unknown sub_app {spec.sub_app!r}")
    if not spec.route:
        raise RegistryError(f"tool {spec.name}: a ToolSpec needs a route")
    route = normalize_route(spec.route)
    extras = tuple(normalize_route(r) for r in spec.extra_routes)
    if spec.effect not in EFFECTS:
        raise RegistryError(f"tool {spec.name}: unknown effect {spec.effect!r}")
    if spec.effect in CONFIRM_EFFECTS and not spec.confirm:
        raise RegistryError(f"tool {spec.name}: effect {spec.effect} needs confirm=True")
    if spec.effect == EFFECT_READ and spec.confirm:
        raise RegistryError(f"tool {spec.name}: a read tool never confirms")
    if spec.effect == EFFECT_READ and route[0] != "GET":
        raise RegistryError(f"tool {spec.name}: a read tool maps to GET routes only")
    if spec.effect == EFFECT_READ and any(r[0] != "GET" for r in extras):
        raise RegistryError(f"tool {spec.name}: a read tool maps to GET routes only")
    if spec.effect == EFFECT_READ and spec.write is not None:
        raise RegistryError(f"tool {spec.name}: a read tool has no write spec")
    if spec.effect in WRITE_SPEC_EFFECTS and spec.write is None:
        # Without a WriteSpec a confirm tool is inert (no routes in
        # turn_allowlist, propose answers not_offered): refuse it here.
        raise RegistryError(f"tool {spec.name}: a {spec.effect} tool needs a write spec")
    housekeeping = spec.effect == EFFECT_HOUSEKEEPING
    for limit_name in ("max_cards_per_turn", "max_proposals_per_turn", "max_tainted_calls_per_turn"):
        limit = getattr(spec, limit_name)
        if limit is None:
            continue
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise RegistryError(f"tool {spec.name}: {limit_name} must be a positive integer or None")
        if limit_name == "max_cards_per_turn" and not spec.confirm:
            raise RegistryError(f"tool {spec.name}: {limit_name} applies to confirm tools only")
        if limit_name == "max_proposals_per_turn" and not (spec.confirm or housekeeping):
            # On a housekeeping tool it bounds the calls, refused or not.
            raise RegistryError(f"tool {spec.name}: {limit_name} applies to confirm and housekeeping tools only")
        if limit_name == "max_tainted_calls_per_turn" and not housekeeping:
            raise RegistryError(f"tool {spec.name}: {limit_name} applies to housekeeping tools only")
    for hook_name in ("redact_args", "precheck"):
        hook = getattr(spec, hook_name)
        if hook is not None and not callable(hook):
            raise RegistryError(f"tool {spec.name}: {hook_name} must be a callable or None")
    if housekeeping:
        # Inline, so nothing a card would check: own rows only, one write
        # route plus GET reads, and memory arguments never in the ledger.
        if spec.confirm:
            raise RegistryError(f"tool {spec.name}: a housekeeping tool never confirms")
        if spec.write is not None:
            raise RegistryError(f"tool {spec.name}: a housekeeping tool has no write spec")
        if spec.namespace not in OWN_ROW_NAMESPACES and spec.name != "email_attachment_to_staging":
            raise RegistryError(
                f"tool {spec.name}: a housekeeping tool lives in the memory or schedule namespace (or report)"
            )
        if route[0] not in HOUSEKEEPING_METHODS:
            raise RegistryError(f"tool {spec.name}: the primary route of a housekeeping tool is a POST, PUT or PATCH")
        if any(r[0] != "GET" for r in extras):
            raise RegistryError(f"tool {spec.name}: the extra routes of a housekeeping tool are GET only")
        if spec.namespace == "memory" and spec.redact_args is None:
            raise RegistryError(f"tool {spec.name}: a memory housekeeping tool needs redact_args")
    if spec.write is not None:
        if not isinstance(spec.write, WriteSpec):
            raise RegistryError(f"tool {spec.name}: write must be a WriteSpec")
        if route[0] == "GET":
            raise RegistryError(f"tool {spec.name}: the primary route of a write tool is the write, not a GET")
        for extra in extras:
            if extra[0] != "GET" and extra not in READ_POST_ROUTES:
                raise RegistryError(
                    f"tool {spec.name}: preview route {extra[0]} {extra[1]} is neither GET nor a read POST"
                )
        for hook in (spec.write.preview_requests, spec.write.build, spec.write.snapshot):
            if not callable(hook):
                raise RegistryError(f"tool {spec.name}: preview_requests, build and snapshot must be callables")
    if not isinstance(spec.schema, dict) or spec.schema.get("type") != "object":
        raise RegistryError(f"tool {spec.name}: schema must be a JSON object schema")
    if spec.schema.get("additionalProperties") is not False:
        raise RegistryError(f"tool {spec.name}: schema must set additionalProperties false")
    props = spec.schema.get("properties") or {}
    if set(spec.schema.get("required") or ()) != set(props):
        raise RegistryError(f"tool {spec.name}: strict schemas require every property")
    if not spec.roles or not spec.roles <= INTERNAL_ROLES:
        raise RegistryError(f"tool {spec.name}: roles must be a non-empty subset of INTERNAL_ROLES")
    if not spec.description.strip() or not spec.progress_label.strip():
        raise RegistryError(f"tool {spec.name}: description and progress_label are required")
    for text in (spec.description, spec.progress_label, json.dumps(spec.schema)):
        if "\u2014" in text:
            raise RegistryError(f"tool {spec.name}: no em dashes in model-facing text")
    if not callable(spec.requests) or not callable(spec.response_trimmer):
        raise RegistryError(f"tool {spec.name}: requests and response_trimmer must be callables")
    _validate_phase4(spec, route, extras, props)
    return ToolSpec(**{**spec.__dict__, "route": route, "extra_routes": extras})


def _validate_phase4(spec: ToolSpec, route: RoutePair, extras: tuple[RoutePair, ...],
                     props: Mapping[str, Any]) -> None:
    """The phase 4 rules of docs/ASSISTANT_PHASE4_PLAN.md 3.1."""
    if spec.effect == EFFECT_SENSITIVE_READ:
        assert spec.write is not None   # WRITE_SPEC_EFFECTS refused it above otherwise
        if route[0] != "POST" or not (route[1].startswith(EVIDENCE_PREFIX) or (spec.name == "read_mailbox" and route[1] == "/assistant/mail/review")):
            raise RegistryError(
                f"tool {spec.name}: a sensitive_read tool writes through a POST under {EVIDENCE_PREFIX}"
            )
        if not spec.roles <= EVIDENCE_ROLES:
            raise RegistryError(f"tool {spec.name}: a sensitive_read tool is for EVIDENCE_ROLES only")
        reason = props.get(REASON_ARG)
        if not isinstance(reason, Mapping) or reason.get("type") != "string":
            raise RegistryError(
                f"tool {spec.name}: a sensitive_read tool needs a required string argument {REASON_ARG!r} "
                f"({REASON_MIN_CHARS} to {REASON_MAX_CHARS} characters, the card's prefill only)"
            )
        if REASON_ARG not in spec.write.bound_text_fields:
            # The proposed reason is model text shown on the card: binding
            # strips it when it first appeared in a tainted result (3.2.1).
            raise RegistryError(f"tool {spec.name}: a sensitive_read tool binds {REASON_ARG!r} as text")
        if spec.write.result_summary is None:
            # The ledger, approval.result and after_snapshot carry only the
            # counts summary, never the rows (3.2.3).
            raise RegistryError(f"tool {spec.name}: a sensitive_read tool needs a result_summary")
    if spec.retention_class in {RETENTION_MAIL, RETENTION_CROSS_MAILBOX} and spec.namespace != "mail":
        raise RegistryError(f"tool {spec.name}: unknown retention_class for this namespace")
    if spec.retention_class not in RETENTION_CLASSES:
        raise RegistryError(f"tool {spec.name}: unknown retention_class {spec.retention_class!r}")
    if (spec.retention_class == RETENTION_PAYROLL_AGGREGATE) != (spec.namespace == "payroll"):
        raise RegistryError(
            f"tool {spec.name}: retention_class payroll_aggregate goes with the payroll namespace, and only there"
        )
    if not isinstance(spec.deferred, bool):
        raise RegistryError(f"tool {spec.name}: deferred must be a bool")
    if spec.deferred and spec.namespace in NEVER_DEFERRED_NAMESPACES:
        raise RegistryError(f"tool {spec.name}: a {spec.namespace} tool is always offered, never deferred")
    if not isinstance(spec.keywords, tuple):
        raise RegistryError(f"tool {spec.name}: keywords must be a tuple of words")
    for word in spec.keywords:
        if not isinstance(word, str) or not word.isascii() or not _KEYWORD_RE.match(word):
            raise RegistryError(f"tool {spec.name}: keyword {word!r} is not one lowercase ASCII word")
    if spec.effect != EFFECT_SENSITIVE_READ:
        for pair in (route, *extras):
            if is_per_person_route(pair) and any(f"{{{key}}}" in pair[1] for key in PERSON_KEYS):
                raise RegistryError(
                    f"tool {spec.name}: {pair[0]} {pair[1]} names one person; only a sensitive_read tool may"
                )


def is_per_person_route(route: RoutePair) -> bool:
    """A route whose answer can be one person's activity (3.1)."""
    path = str(route[1] or "")
    return path in PER_PERSON_PATHS or path.startswith(EVIDENCE_PREFIX)


def _names_person(value: Any) -> bool:
    """True when a person key holds a non-empty value at any depth."""
    if isinstance(value, Mapping):
        for key, inner in value.items():
            if key in PERSON_KEYS and inner not in (None, "", [], {}):
                return True
            if _names_person(inner):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(_names_person(v) for v in value)
    return False


def per_person_violation(effect: str, request: ToolRequest) -> str | None:
    """Why `request` may not be sent by a spec of `effect`, or None.

    Only a sensitive_read spec (its card carries the human's reason and
    writes the access log) may name one person, by actor_id, user_id or
    file_id in the query, the JSON body or the path template, on
    /analytics/file-access, /analytics/activity, the project notification
    log or any /analytics/evidence/ route (3.1). The executor applies this
    to every request it sends; tests apply it to the requests a spec builds
    from its fixtures."""
    if effect == EFFECT_SENSITIVE_READ or not is_per_person_route(request.route):
        return None
    if any(f"{{{key}}}" in request.route[1] for key in PERSON_KEYS):
        return f"{request.route[1]} names one person"
    if _names_person(dict(request.params or {})) or _names_person(request.json):
        return f"{request.route[1]} was sent a person filter"
    return None


def register(spec: ToolSpec) -> ToolSpec:
    spec = validate(spec)
    existing = _REGISTRY.get(spec.name)
    if existing is not None and existing != spec:
        raise RegistryError(f"tool {spec.name} is already registered")
    _REGISTRY[spec.name] = spec
    return spec


def _load() -> None:
    global _LOADED
    if _LOADED:
        return
    # The tool modules register on import; importing them here keeps this
    # module free of the tools' own imports.
    from app.assistant import tools as _tools  # noqa: F401

    _tools.load_all()
    _LOADED = True


def all_tools() -> dict[str, ToolSpec]:
    _load()
    return dict(_REGISTRY)


def get(name: str) -> ToolSpec | None:
    _load()
    return _REGISTRY.get(name)


def toolset_for(
    user: Any,
    enabled_map: Mapping[str, bool],
    *,
    settings_row: Mapping[str, Any] | None = None,
    mode: str = MODE_NORMAL,
    incognito: bool = False,
) -> list[ToolSpec]:
    """The tools offered to `user` right now, sorted by name.

    Filters, in order: the role must be internal and on the spec (an
    external estimator never gets a toolset, Permissions item 3); the
    spec's sub_app must be on in `enabled_map` (item 10; spine tools have
    none); the tool must not be in assistant_settings.disabled_tools (item
    15); in Mailbox review mode only read and sensitive_read tools remain
    and every memory or schedule tool is dropped whatever its effect
    (Prompt injection and taint); an Incognito turn has no memory tools
    (phase 3, 3.7.1)."""
    from app.assistant.access import disabled_tools

    try:
        role = Role(getattr(user, "role", None))
    except ValueError:
        return []
    if role not in INTERNAL_ROLES or mode not in {MODE_NORMAL, MODE_MAILBOX_REVIEW}:
        return []
    if mode == MODE_MAILBOX_REVIEW and role not in EVIDENCE_ROLES:
        return []
    off = disabled_tools(settings_row)
    out: list[ToolSpec] = []
    for spec in all_tools().values():
        if role not in spec.roles:
            continue
        if spec.sub_app is not None and not enabled_map.get(spec.sub_app, False):
            continue
        if spec.name in off:
            continue
        if mode == MODE_MAILBOX_REVIEW and (spec.effect not in READ_EFFECTS
                                            or spec.namespace in OWN_ROW_NAMESPACES or spec.namespace in {"web", "browser"}
                                            or (spec.namespace == "mail" and spec.name != "read_mailbox")):
            continue
        if spec.name == "read_mailbox" and mode != MODE_MAILBOX_REVIEW:
            continue
        if incognito and spec.namespace == "memory":
            continue
        if not spec.offered:
            continue
        out.append(spec)
    return sorted(out, key=lambda s: s.name)


def tool_definitions(specs: Iterable[ToolSpec]) -> list[dict]:
    """Function tool definitions for the model, sorted by name with sorted
    keys: the same toolset always serializes to the same bytes."""
    return [s.definition() for s in sorted(specs, key=lambda s: s.name)]


def definitions_bytes(specs: Iterable[ToolSpec]) -> bytes:
    return json.dumps(tool_definitions(specs), sort_keys=True, separators=(",", ":")).encode("utf-8")


def allowlist_for(specs: Iterable[ToolSpec]) -> frozenset[RoutePair]:
    """Every (METHOD, path_format) the toolset may call: the shim's allowlist."""
    pairs: set[RoutePair] = set()
    for spec in specs:
        pairs.update(spec.routes)
    return frozenset(pairs)


def turn_allowlist(specs: Iterable[ToolSpec]) -> frozenset[RoutePair]:
    """Read and housekeeping specs: all routes. Write specs: extra_routes
    only (previews). The write route of a confirm tool is never reachable
    inside a turn. A confirm spec without a write spec contributes nothing."""
    pairs: set[RoutePair] = set()
    for spec in specs:
        if spec.effect in (EFFECT_READ, EFFECT_HOUSEKEEPING):
            pairs.update(spec.routes)
        elif spec.write is not None:
            pairs.update(spec.extra_routes)
    return frozenset(pairs)


def execute_allowlist(spec: ToolSpec) -> frozenset[RoutePair]:
    """The approval execution's allowlist: exactly the approved tool's routes."""
    return frozenset(spec.routes)


# ── Schema helpers for the tool modules ─────────────────────────────────────


def schema(properties: Mapping[str, dict]) -> dict:
    """A strict object schema: every property required (optional ones are
    nullable), additionalProperties false, keys sorted."""
    return _sorted({
        "type": "object",
        "properties": dict(properties),
        "required": sorted(properties),
        "additionalProperties": False,
    })


def nullable(inner: dict) -> dict:
    kinds = inner.get("type")
    types = [kinds] if isinstance(kinds, str) else list(kinds or [])
    if "null" not in types:
        types.append("null")
    return {**inner, "type": types}


RESPONSE_FORMAT_PROP: dict = {
    "type": "string",
    "enum": list(RESPONSE_FORMATS),
    "description": "concise (default, fewest fields) or detailed.",
}
LIMIT_PROP: dict = nullable({
    "type": "integer",
    "description": f"Most items to return, 1 to {MAX_LIMIT}; null means {DEFAULT_LIMIT}.",
})


def common_args(args: Mapping[str, Any]) -> tuple[str, int]:
    """(response_format, limit) from a tool call's arguments, defaulted."""
    fmt = str(args.get("response_format") or "concise")
    if fmt not in RESPONSE_FORMATS:
        fmt = "concise"
    raw = args.get("limit")
    try:
        limit = int(raw) if raw is not None else DEFAULT_LIMIT
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    return fmt, max(1, min(MAX_LIMIT, limit))
