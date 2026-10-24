"""WS8 chat checks: 12 scripted questions against one AI Estimating run
(docs/AI_ESTIMATING_BUILD_SPEC.md section 6 WS8 acceptance, live $3 on
gpt-6-astra, feature plans_chat).

The script never calls a model itself. It drives the dev sweep backend's
chat routes like the page does: create a thread per question
(POST /ai-estimating/runs/{run}/chat/threads), post the question
(POST .../threads/{thread}/messages, answered by a queued plans_chat job)
and poll the thread (GET .../threads/{thread}) until the assistant message
is done or failed. Each question carries an expectation (regexes on the
answer, the tools the answer used, the message's pricing_restricted tag)
and is scored pass or fail; a table is printed and written.

Questions (defaults; text and truth can be refined per run with a
questions file of schema bdr.eval.chat_checks/1 under EVAL_ROOT, see
`load_questions`): counts on a sheet, a legend lookup, a schedule cell, a
scope exclusion, a measured length, a linear fixture, an RFI, a deliverable,
a spec requirement, a question the outputs cannot answer (a refusal, no
invented number), and explain_row twice: from a pricing actor (an
Executive with AI pricing open to them: pricing_restricted true) and from
an engineer under AI_PRICING_STATE=test (no price block: pricing_restricted
false, no dollar figure; the restricted pricing clause is the system rule
that produces it).

Roles. The backend has no role impersonation for internal roles (core/deps:
only X-Impersonate-Estimator, the external estimator portal, honored for
dev callers), so a question runs as the role of the session that posts it.
The default session is the dev E2E account (password grant, the
plans_reader_live_run._Auth recipe; the account is an Executive). A question
that needs another role runs only when a session of that role is given with
`--role-token engineer=ENV_VAR` (an access token of an engineer account in
that environment variable); otherwise it is reported as skipped, never as
passed. GET /users/me and GET /features (ai_pricing) of each session are
checked before a role-gated question runs.

Usage (from bdr_be):

    # $0: print the questions and expectations; no network, no call.
    uv run python scripts/plans_reader_chat_checks.py --dry-run
    # Live (wave 4): the sweep backend on --base-url with an open WS8 sweep
    # (the backend arms its own ledger; this script makes no model call).
    uv run python scripts/plans_reader_chat_checks.py --run-id <run uuid> \\
        --base-url http://127.0.0.1:5061 [--sweep <open sweep id>] \\
        [--role-token engineer=ENGINEER_TOKEN] [--questions FILE] [--out DIR]

A live run refuses without --run-id, when GET /ai-estimating/status shows
the ledger not enforced or no open sweep (or a sweep other than --sweep),
when the sweep has less left than --budget, and it stops posting questions
once the answers so far plus one more question's cap would pass --budget.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

QUESTIONS_SCHEMA = "bdr.eval.chat_checks/1"
RESULTS_SCHEMA = "bdr.eval.chat_checks_results/1"
FEATURE = "plans_chat"
CHAT_MODEL = "gpt-6-astra"
WORKSTREAM = "WS8"
BUDGET_USD = Decimal("3")
# Planning cap of one answer (a chat loop of a few tool rounds on
# gpt-6-astra); the script stops before a question that could pass the budget.
PER_QUESTION_CAP_USD = Decimal("0.25")
ROLES: tuple[str, ...] = ("any", "pricing", "engineer")
ENGINEER_ROLES = frozenset({"estimating_engineer_materials", "estimating_engineer_labor"})
TERMINAL = ("done", "failed")
EN_DASH = chr(0x2013)
EM_DASH = chr(0x2014)

DEFAULT_PARAMS: dict[str, str] = {
    "count_sheet": "E2.01",
    "count_item": "duplex receptacles",
    "legend_symbol": "GFCI receptacle",
    "schedule_name": "lighting fixture schedule",
    "fixture_type": "A1",
    "exclusion_topic": "fire alarm",
    "length_item": "branch circuit conduit",
    "linear_fixture": "linear LED strip fixtures",
    "spec_topic": "wiring devices",
    "line": "B1",
}

NUM = r"\b\d[\d,]*(?:\.\d+)?\b"
FEET = r"\b\d[\d,]*(?:\.\d+)?\s*(?:ft\b|feet\b|foot\b|lf\b|linear f(?:ee|oo)t|')"
SHEET = r"\b[A-Z]{1,3}-?(?:\d{1,3}\.\d{1,3}|\d{3})[A-Z]?\b"
SPEC_SECTION = r"\b\d{2}\s?\d{2}\s?\d{2}(?:\.\d+)?\b"
DOLLAR = r"\$\s?\d"
REFUSAL = (
    r"\b(?:not (?:in|found|available|included|stated|specified|recorded|covered|part of)|no (?:information|record|"
    r"data|mention|way)|does(?:n'?t| not) (?:say|show|include|contain|have|cover|record|list)|"
    r"do(?:n'?t| not) (?:have|know|see)|cannot|can'?t|isn'?t|unable|outside (?:the|what))"
)
HOURS_CLAIM = r"\b\d+(?:\.\d+)?\s*(?:hours|hrs|man.?hours|crew.?hours)\b"
# Tools no check may use: the chat must never change a deliverable here.
NEVER_TOOLS: tuple[str, ...] = ("modify_deliverable",)

COUNT_TOOLS = ["get_output", "search_outputs", "get_deliverable", "census", "text_search", "get_rows", "get_row",
               "explain_row", "get_brief"]
READ_TOOLS = ["get_output", "search_outputs", "get_deliverable", "get_brief", "list_documents"]


class ChatCheckError(RuntimeError):
    """The checks cannot go ahead as asked (usage, preflight)."""


# ---------------------------------------------------------------------------
# Questions and expectations
# ---------------------------------------------------------------------------


@dataclass
class Expect:
    answer_all: list[str] = field(default_factory=list)
    answer_any: list[str] = field(default_factory=list)
    answer_none: list[str] = field(default_factory=list)
    tools_any: list[str] = field(default_factory=list)
    tools_all: list[str] = field(default_factory=list)
    tools_none: list[str] = field(default_factory=list)
    pricing_restricted: bool | None = None
    refusal: bool = False

    def describe(self) -> list[str]:
        out: list[str] = []
        for name in ("answer_all", "answer_any", "answer_none", "tools_any", "tools_all", "tools_none"):
            vals = getattr(self, name)
            if vals:
                out.append(f"{name}: {', '.join(vals)}")
        if self.pricing_restricted is not None:
            out.append(f"pricing_restricted: {str(self.pricing_restricted).lower()}")
        if self.refusal:
            out.append("refusal: answer says the outputs do not hold it and invents no hours figure")
        out.append("always: status done, no em or en dash, never " + ", ".join(NEVER_TOOLS))
        return out


@dataclass
class Question:
    id: str
    topic: str
    text: str
    role: str = "any"
    expect: Expect = field(default_factory=Expect)
    note: str | None = None


def default_questions(params: Mapping[str, str] | None = None) -> list[Question]:
    """The 12 WS8 questions with expectations that hold on any run; a
    questions file adds the run's truth (exact counts, lengths, sections)."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    esc = {k: re.escape(v) for k, v in p.items()}
    explain = f"Explain where BOQ line {p['line']} comes from: its quantity, the sources behind it and its price."
    return [
        Question("count_sheet", "count on a sheet",
                 f"How many {p['count_item']} are shown on sheet {p['count_sheet']}? Give the number and the sheet you counted from.",
                 expect=Expect(answer_all=[NUM, esc["count_sheet"]], tools_any=COUNT_TOOLS)),
        Question("legend_lookup", "legend lookup",
                 f"What does the {p['legend_symbol']} symbol mean in the drawing legend, and which sheet is the legend on?",
                 expect=Expect(answer_all=[SHEET], tools_any=["legend_lookup", "text_search", *READ_TOOLS])),
        Question("schedule_cell", "schedule cell",
                 f"In the {p['schedule_name']}, what is the description and the voltage of fixture type {p['fixture_type']}?",
                 expect=Expect(answer_all=[rf"\b{esc['fixture_type']}\b", r"\b(?:120|208|240|277|347|480)\b|volt|mvolt|universal"],
                               tools_any=["get_schedule", "text_search", *READ_TOOLS])),
        Question("scope_exclusion", "scope exclusion",
                 f"Is {p['exclusion_topic']} work in our scope or excluded? Say where that is stated.",
                 expect=Expect(answer_all=[esc["exclusion_topic"]],
                               answer_any=[r"exclu", r"by others", r"not included", r"\bNIC\b", r"not in contract",
                                           r"\bincluded\b", r"in (?:our )?scope"],
                               tools_any=["get_scope_policy", "spec_lookup", *READ_TOOLS])),
        Question("measured_length", "measured length",
                 f"How many feet of {p['length_item']} did the run measure, and from which sheets?",
                 expect=Expect(answer_all=[FEET, SHEET], tools_any=["explain_row", "get_rows", "get_row", *READ_TOOLS])),
        Question("linear_fixture", "linear fixture",
                 f"What is the total length of {p['linear_fixture']} on the plans, in feet?",
                 expect=Expect(answer_all=[FEET], tools_any=["explain_row", "get_rows", "get_schedule", *READ_TOOLS])),
        Question("rfi", "RFI",
                 "What RFIs or open questions did the run raise for the GC? List them.",
                 expect=Expect(answer_all=[r"\bRFI|question"], tools_any=READ_TOOLS)),
        Question("deliverable", "deliverable",
                 "Which deliverables did this run produce, and what version is the BOQ?",
                 expect=Expect(answer_all=[r"\bBOQ\b", r"\bv(?:ersion)?\s*\d+"],
                               tools_any=["get_deliverable", "get_brief", "list_documents", "get_output"])),
        Question("spec_requirement", "spec requirement",
                 f"What does the specification require for {p['spec_topic']}? Cite the spec section number.",
                 expect=Expect(answer_all=[SPEC_SECTION], tools_any=["spec_lookup", "text_search", *READ_TOOLS])),
        Question("unanswerable", "cannot be answered from the outputs",
                 "How many hours did the electricians work on site last week, and what did they install?",
                 expect=Expect(refusal=True)),
        Question("explain_row_pricing", "explain_row as a pricing actor", explain, role="pricing",
                 expect=Expect(tools_all=["explain_row"], pricing_restricted=True),
                 note="needs AI_PRICING_ENABLED and AI_PRICING_CHAT_TOOLS_ENABLED on the backend, and a session "
                      "whose /features.ai_pricing is true (an Executive under AI_PRICING_STATE=test)"),
        Question("explain_row_engineer", "explain_row as an engineer under ai_pricing_state=test", explain,
                 role="engineer",
                 expect=Expect(tools_all=["explain_row"], pricing_restricted=False, answer_none=[DOLLAR]),
                 note="needs an engineer session (--role-token engineer=ENV_VAR) whose /features.ai_pricing is false"),
    ]


_EXPECT_LISTS = ("answer_all", "answer_any", "answer_none", "tools_any", "tools_all", "tools_none")


def load_questions(path: Path | None, params: Mapping[str, str] | None = None) -> tuple[str, list[Question]]:
    """The defaults, refined by a questions file when given:
    {"schema": "bdr.eval.chat_checks/1", "name": ..., "params": {...},
     "questions": [{"id": <default id>, "text"?: ..., "note"?: ...,
                    "expect"?: {<list fields extend the default>,
                                "pricing_restricted"?: bool, "replace"?: bool}}]}
    Params on the command line win over the file's. Unknown ids refuse."""
    merged = dict(DEFAULT_PARAMS)
    name = "default"
    overrides: list[Mapping[str, Any]] = []
    if path is not None:
        p = Path(path)
        if not p.is_file():
            raise ChatCheckError(f"questions file {p} not found")
        doc = json.loads(p.read_text())
        if doc.get("schema") != QUESTIONS_SCHEMA:
            raise ChatCheckError(f"questions file {p} is not {QUESTIONS_SCHEMA}")
        name = str(doc.get("name") or p.stem)
        merged.update({str(k): str(v) for k, v in (doc.get("params") or {}).items()})
        overrides = [o for o in doc.get("questions") or [] if isinstance(o, Mapping)]
    merged.update(params or {})
    unknown = sorted(set(merged) - set(DEFAULT_PARAMS))
    if unknown:
        raise ChatCheckError(f"unknown params {unknown}; known: {sorted(DEFAULT_PARAMS)}")
    questions = default_questions(merged)
    by_id = {q.id: q for q in questions}
    for o in overrides:
        q = by_id.get(str(o.get("id")))
        if q is None:
            raise ChatCheckError(f"questions file names unknown question {o.get('id')!r}; known: {sorted(by_id)}")
        if o.get("text"):
            q.text = str(o["text"])
        if o.get("note"):
            q.note = str(o["note"])
        ex = o.get("expect") or {}
        if not isinstance(ex, Mapping):
            raise ChatCheckError(f"question {q.id}: expect must be an object")
        if ex.get("replace"):
            q.expect = Expect(refusal=bool(ex.get("refusal", False)))
        for key in _EXPECT_LISTS:
            extra = ex.get(key)
            if extra:
                getattr(q.expect, key).extend(str(v) for v in extra)
        if "pricing_restricted" in ex:
            q.expect.pricing_restricted = None if ex["pricing_restricted"] is None else bool(ex["pricing_restricted"])
    for q in questions:
        for key in ("answer_all", "answer_any", "answer_none"):
            for rx in getattr(q.expect, key):
                try:
                    re.compile(rx)
                except re.error as exc:
                    raise ChatCheckError(f"question {q.id}: bad regex {rx!r} ({exc})") from exc
    return name, questions


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def tools_used(message: Mapping[str, Any]) -> list[str]:
    calls = message.get("tool_calls") or []
    return [str(c.get("name")) for c in calls if isinstance(c, Mapping) and c.get("name")]


def _found(rx: str, text: str) -> bool:
    return re.search(rx, text, re.IGNORECASE) is not None


def score(question: Question, message: Mapping[str, Any] | None) -> dict[str, Any]:
    """Pass or fail of one answer: `failures` lists every unmet part."""
    if not message:
        return {"passed": False, "failures": ["no assistant message"], "tools": [], "pricing_restricted": None}
    ex = question.expect
    text = str(message.get("content") or "")
    used = tools_used(message)
    failures: list[str] = []
    status = message.get("status")
    if status != "done":
        failures.append(f"status {status}" + (f" ({message.get('error')})" if message.get("error") else ""))
    if EN_DASH in text or EM_DASH in text:
        failures.append("answer contains an em or en dash")
    for rx in ex.answer_all:
        if not _found(rx, text):
            failures.append(f"answer lacks /{rx}/")
    if ex.answer_any and not any(_found(rx, text) for rx in ex.answer_any):
        failures.append("answer matches none of " + ", ".join(f"/{rx}/" for rx in ex.answer_any))
    for rx in ex.answer_none:
        if _found(rx, text):
            failures.append(f"answer has /{rx}/")
    if ex.refusal:
        if not _found(REFUSAL, text):
            failures.append("answer does not say the outputs cannot answer it")
        if _found(HOURS_CLAIM, text):
            failures.append("answer invents an hours figure")
    if ex.tools_any and not any(t in used for t in ex.tools_any):
        failures.append("used none of the tools " + ", ".join(ex.tools_any))
    for t in ex.tools_all:
        if t not in used:
            failures.append(f"did not use {t}")
    for t in [*ex.tools_none, *NEVER_TOOLS]:
        if t in used:
            failures.append(f"used {t}")
    restricted = bool(message.get("pricing_restricted"))
    if ex.pricing_restricted is not None and restricted != ex.pricing_restricted:
        failures.append(f"pricing_restricted {str(restricted).lower()}, expected {str(ex.pricing_restricted).lower()}")
    return {"passed": not failures, "failures": failures, "tools": used, "pricing_restricted": restricted}


# ---------------------------------------------------------------------------
# Sessions and the HTTP seam
# ---------------------------------------------------------------------------


@dataclass
class Session:
    """One signed-in caller. `role` and `ai_pricing` come from GET
    /users/me and GET /features (filled by `describe_session`)."""

    name: str
    headers: Callable[[], dict[str, str]]
    auth: Any = None
    role: str | None = None
    ai_pricing: bool | None = None

    def fits(self, need: str) -> bool:
        if need == "any":
            return True
        if need == "pricing":
            return self.ai_pricing is True
        if need == "engineer":
            return (self.role or "") in ENGINEER_ROLES and self.ai_pricing is False
        return False


class Api:
    """The backend over HTTP (httpx). `call` returns (status, json or text)."""

    def __init__(self, base_url: str, client: Any = None) -> None:
        import httpx

        self.base = base_url.rstrip("/")
        self.client = client or httpx.Client(timeout=120)

    def call(self, session: Session, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        url = f"{self.base}{path}"
        kw: dict[str, Any] = {} if body is None else {"json": body}
        if session.auth is not None:
            r = session.auth.retry_401(self.client, method, url, **kw)
        else:
            r = self.client.request(method, url, headers=session.headers(), **kw)
        if r.status_code == 429:
            time.sleep(min(float(r.headers.get("Retry-After", "10") or 10), 60))
            if session.auth is not None:
                r = session.auth.retry_401(self.client, method, url, **kw)
            else:
                r = self.client.request(method, url, headers=session.headers(), **kw)
        try:
            data = r.json()
        except ValueError:
            data = r.text
        return r.status_code, data


def describe_session(api: Any, session: Session) -> Session:
    code, me = api.call(session, "GET", "/users/me")
    if code < 300 and isinstance(me, Mapping):
        session.role = str(me.get("role") or "") or None
    code, feats = api.call(session, "GET", "/features")
    if code < 300 and isinstance(feats, Mapping):
        flags = feats.get("flags") if isinstance(feats.get("flags"), Mapping) else feats
        if "ai_pricing" in flags:
            session.ai_pricing = bool(flags.get("ai_pricing"))
    return session


def parse_role_tokens(items: Sequence[str] | None, environ: Mapping[str, str] | None = None) -> list[Session]:
    """`--role-token engineer=ENV_VAR`: an access token read from the named
    environment variable (never from the command line itself)."""
    env = os.environ if environ is None else environ
    out: list[Session] = []
    for item in items or []:
        if "=" not in item:
            raise ChatCheckError(f"--role-token {item!r} must be ROLE=ENV_VAR")
        role, var = (s.strip() for s in item.split("=", 1))
        if role not in ROLES or role == "any":
            raise ChatCheckError(f"--role-token role must be pricing or engineer, got {role!r}")
        token = (env.get(var) or "").strip()
        if not token:
            raise ChatCheckError(f"--role-token {role}: environment variable {var} is empty")
        out.append(Session(name=f"{role} token ({var})", headers=lambda t=token: {"Authorization": f"Bearer {t}"}))
    return out


def pick_session(sessions: Sequence[Session], need: str) -> Session | None:
    for s in sessions:
        if s.fits(need):
            return s
    return None


# ---------------------------------------------------------------------------
# Running the questions
# ---------------------------------------------------------------------------


@dataclass
class QuestionResult:
    id: str
    topic: str
    role: str
    session: str | None = None
    session_role: str | None = None
    status: str = "skipped"  # passed | failed | skipped | error
    failures: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    pricing_restricted: bool | None = None
    answer: str | None = None
    cost_usd: float = 0.0
    input_tokens: int | None = None
    output_tokens: int | None = None
    model: str | None = None
    thread_id: str | None = None
    message_id: str | None = None
    seconds: float | None = None
    note: str | None = None


def _id_of(data: Any, *keys: str) -> str | None:
    if not isinstance(data, Mapping):
        return None
    if data.get("id"):
        return str(data["id"])
    for k in keys:
        inner = data.get(k)
        if isinstance(inner, Mapping) and inner.get("id"):
            return str(inner["id"])
    return None


def ask(api: Any, session: Session, run_id: str, q: Question, *, poll_s: float, max_wait_s: float,
        sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time) -> QuestionResult:
    res = QuestionResult(id=q.id, topic=q.topic, role=q.role, session=session.name, session_role=session.role,
                         note=q.note)
    base = f"/ai-estimating/runs/{run_id}/chat/threads"
    code, thread = api.call(session, "POST", base, {"title": f"WS8 chat check {q.id}"[:120]})
    tid = _id_of(thread, "thread")
    if code >= 300 or not tid:
        res.status, res.failures = "error", [f"create thread {code}: {str(thread)[:200]}"]
        return res
    res.thread_id = tid
    started = clock()
    code, sent = api.call(session, "POST", f"{base}/{tid}/messages", {"content": q.text, "pro_max": False})
    mid = _id_of(sent.get("assistant_message") if isinstance(sent, Mapping) else None)
    if code >= 300 or not mid:
        res.status, res.failures = "error", [f"send message {code}: {str(sent)[:200]}"]
        return res
    res.message_id = mid
    message: Mapping[str, Any] | None = None
    while True:
        code, payload = api.call(session, "GET", f"{base}/{tid}")
        if code < 300 and isinstance(payload, Mapping):
            message = next((m for m in payload.get("messages") or []
                            if isinstance(m, Mapping) and str(m.get("id")) == mid), None)
            if message and message.get("status") in TERMINAL:
                break
        if clock() - started > max_wait_s:
            res.status = "error"
            res.failures = [f"no answer within {int(max_wait_s)} s (last status "
                            f"{(message or {}).get('status')})"]
            res.seconds = round(clock() - started, 1)
            return res
        sleep(poll_s)
    res.seconds = round(clock() - started, 1)
    verdict = score(q, message)
    res.status = "passed" if verdict["passed"] else "failed"
    res.failures, res.tools, res.pricing_restricted = verdict["failures"], verdict["tools"], verdict["pricing_restricted"]
    res.answer = str(message.get("content") or "")[:4000]
    res.cost_usd = float(message.get("cost_usd") or 0)
    res.input_tokens, res.output_tokens = message.get("input_tokens"), message.get("output_tokens")
    res.model = message.get("model")
    return res


def run_checks(questions: Sequence[Question], sessions: Sequence[Session], api: Any, run_id: str, *,
               budget: Decimal = BUDGET_USD, per_question_cap: Decimal = PER_QUESTION_CAP_USD,
               poll_s: float = 3.0, max_wait_s: float = 420.0, sleep: Callable[[float], None] = time.sleep,
               clock: Callable[[], float] = time.time, log: Callable[[str], None] = print) -> list[QuestionResult]:
    """Ask every question with a session that fits its role; skip the rest
    (reason recorded) and stop posting once the budget could be passed."""
    results: list[QuestionResult] = []
    spent = Decimal("0")
    for q in questions:
        session = pick_session(sessions, q.role)
        if session is None:
            have = ", ".join(f"{s.name} ({s.role}, ai_pricing {s.ai_pricing})" for s in sessions) or "none"
            res = QuestionResult(id=q.id, topic=q.topic, role=q.role, note=q.note,
                                 failures=[f"no session fits role {q.role}; sessions: {have}"])
        elif spent + per_question_cap > budget:
            res = QuestionResult(id=q.id, topic=q.topic, role=q.role, note=q.note,
                                 failures=[f"budget: ${spent:.2f} spent, one more answer could pass ${budget:.2f}"])
        else:
            res = ask(api, session, run_id, q, poll_s=poll_s, max_wait_s=max_wait_s, sleep=sleep, clock=clock)
            spent += Decimal(str(res.cost_usd or 0))
        results.append(res)
        log(f"{q.id}: {res.status.upper()} cost {res.cost_usd:.4f}"
            + (f" ({'; '.join(res.failures)})" if res.failures else ""))
    return results


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def format_questions(questions: Sequence[Question], *, budget: Decimal, name: str) -> list[str]:
    lines = [f"WS8 chat checks ({name}): {len(questions)} questions, feature {FEATURE} on {CHAT_MODEL}, "
             f"budget ${budget:.2f} (stops when one more answer at ${PER_QUESTION_CAP_USD:.2f} could pass it)"]
    for i, q in enumerate(questions, 1):
        lines.append(f"{i:2d}. [{q.id}] role {q.role}: {q.text}")
        for e in q.expect.describe():
            lines.append(f"      expect {e}")
        if q.note:
            lines.append(f"      note: {q.note}")
    lines.append("roles: no internal-role impersonation exists on the backend (only X-Impersonate-Estimator); "
                 "questions run as the posting session's role; role-gated ones need --role-token or are skipped")
    lines.append("model calls made by this script: 0 (the sweep backend answers)")
    return lines


def _cell(text: Any, limit: int = 60) -> str:
    s = re.sub(r"\s+", " ", str(text or "")).replace("|", "/")
    return s if len(s) <= limit else s[: limit - 3] + "..."


def format_table(results: Sequence[QuestionResult]) -> list[str]:
    lines = ["| # | question | role (session) | status | tools | pricing_restricted | $ | why |",
             "|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(results, 1):
        lines.append("| " + " | ".join([
            str(i), r.id, f"{r.role} ({r.session_role or '-'})", r.status.upper(), _cell(", ".join(r.tools), 50),
            "-" if r.pricing_restricted is None else str(r.pricing_restricted).lower(), f"{r.cost_usd:.4f}",
            _cell("; ".join(r.failures), 120) or "-",
        ]) + " |")
    counts = {k: sum(1 for r in results if r.status == k) for k in ("passed", "failed", "skipped", "error")}
    total = sum(r.cost_usd for r in results)
    lines.append("")
    lines.append(f"passed {counts['passed']}, failed {counts['failed']}, skipped {counts['skipped']}, "
                 f"error {counts['error']} of {len(results)}; spent ${total:.4f}")
    return lines


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="WS8 scripted chat checks against a running sweep backend.")
    p.add_argument("--run-id", default=None, help="the AI Estimating run to ask about (required live)")
    p.add_argument("--base-url", default="http://127.0.0.1:5061")
    p.add_argument("--dry-run", action="store_true", help="print the questions and expectations; no network ($0)")
    p.add_argument("--sweep", default=None, help="the open sweep id the backend must show (optional check)")
    p.add_argument("--budget", type=Decimal, default=BUDGET_USD, help="dollar cap of the answers (spec: $3)")
    p.add_argument("--per-question-cap", type=Decimal, default=PER_QUESTION_CAP_USD)
    p.add_argument("--questions", type=Path, default=None, help="questions file (schema bdr.eval.chat_checks/1)")
    p.add_argument("--param", action="append", default=None, help="KEY=VALUE question parameter (repeatable)")
    p.add_argument("--only", default=None, help="comma list of question ids")
    p.add_argument("--role-token", action="append", default=None,
                   help="ROLE=ENV_VAR: a session for a role-gated question (pricing or engineer)")
    p.add_argument("--no-e2e", action="store_true", help="do not sign in the E2E account (role tokens only)")
    p.add_argument("--poll-seconds", type=float, default=3.0)
    p.add_argument("--max-wait-seconds", type=float, default=420.0)
    p.add_argument("--eval-root", type=Path, default=None)
    p.add_argument("--out", type=Path, default=None,
                   help="output directory (default EVAL_ROOT/sweeps/<sweep>/chat_checks_<time>)")
    return p


def _params(items: Sequence[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items or []:
        if "=" not in item:
            raise ChatCheckError(f"--param {item!r} must be KEY=VALUE")
        k, v = item.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def preflight(api: Any, session: Session, *, sweep: str | None, budget: Decimal) -> dict[str, Any]:
    """The backend's ledger must enforce with an open sweep that has the
    budget left (GET /ai-estimating/status)."""
    code, st = api.call(session, "GET", "/ai-estimating/status")
    if code >= 300 or not isinstance(st, Mapping):
        raise ChatCheckError(f"GET /ai-estimating/status answered {code}: {str(st)[:200]}")
    ledger = st.get("eval_ledger") or {}
    if not ledger.get("enforced"):
        raise ChatCheckError("the backend's eval ledger is not enforced (AI_EVAL_LEDGER_ENFORCE=true on the sweep backend)")
    open_id = ledger.get("open_sweep_id")
    if not open_id:
        raise ChatCheckError("no sweep is open on the backend; open a WS8 sweep first (scripts/eval_ledger.py open)")
    if sweep and str(open_id) != str(sweep):
        raise ChatCheckError(f"the backend's open sweep is {open_id}, not {sweep}")
    left = ledger.get("sweep_remaining")
    if left is not None and Decimal(str(left)) < budget:
        raise ChatCheckError(f"sweep {open_id} has ${Decimal(str(left)):.2f} left, under the ${budget:.2f} budget")
    if not st.get("enabled"):
        raise ChatCheckError(f"AI Estimating is not enabled on the backend: {st.get('unavailable_reason')}")
    return {"open_sweep_id": str(open_id), "sweep_remaining": left, "model": st.get("model"),
            "reasoning_effort": st.get("reasoning_effort")}


def main(argv: list[str] | None = None, *, api: Any = None, sessions: Sequence[Session] | None = None,
         sleep: Callable[[float], None] = time.sleep, environ: Mapping[str, str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    eval_root = args.eval_root
    if eval_root is None:
        from app.core.config import get_settings

        eval_root = Path(getattr(get_settings(), "eval_root", "") or "/Users/thomasmoore/Desktop/BDR/backups/eval")
    e2e_env: dict[str, str] | None = None
    try:
        name, questions = load_questions(args.questions, _params(args.param))
        if args.only:
            wanted = {t.strip() for t in args.only.split(",") if t.strip()}
            missing = wanted - {q.id for q in questions}
            if missing:
                raise ChatCheckError(f"unknown question ids {sorted(missing)}")
            questions = [q for q in questions if q.id in wanted]
        if args.budget <= 0 or args.per_question_cap <= 0:
            raise ChatCheckError("--budget and --per-question-cap must be positive")
        for line in format_questions(questions, budget=args.budget, name=name):
            print(line)
        if args.dry_run:
            if args.out:
                args.out.mkdir(parents=True, exist_ok=True)
                (args.out / "questions.json").write_text(json.dumps([asdict(q) for q in questions], indent=1))
                print(f"questions: {args.out / 'questions.json'}")
            return 0
        if not args.run_id:
            raise ChatCheckError("a live check needs --run-id <run uuid>; use --dry-run for the questions")
        if api is None:
            api = Api(args.base_url)
        if sessions is None:
            built = parse_role_tokens(args.role_token, environ)
            if not args.no_e2e:
                from scripts.plans_reader_live_run import _Auth, _load_env

                e2e_env = _load_env()
                if "bpidntbyvoooqvaispup" not in e2e_env.get("SUPABASE_URL", ""):
                    raise ChatCheckError("the .env SUPABASE_URL is not the dev project; refusing to sign in")
                auth = _Auth(e2e_env)
                built.insert(0, Session(name="e2e", headers=auth.headers, auth=auth))
            sessions = built
        if not sessions:
            raise ChatCheckError("no session: drop --no-e2e or give --role-token")
        for s in sessions:
            describe_session(api, s)
            print(f"session {s.name}: role {s.role}, ai_pricing {s.ai_pricing}")
        pre = preflight(api, sessions[0], sweep=args.sweep, budget=args.budget)
        print(f"backend: sweep {pre['open_sweep_id']} remaining {pre['sweep_remaining']}, model {pre['model']}")
        started = time.time()
        results = run_checks(questions, sessions, api, args.run_id, budget=args.budget,
                             per_question_cap=args.per_question_cap, poll_s=args.poll_seconds,
                             max_wait_s=args.max_wait_seconds, sleep=sleep)
        table = format_table(results)
        out_dir = args.out or eval_root / "sweeps" / pre["open_sweep_id"] / f"chat_checks_{time.strftime('%Y%m%d_%H%M%S')}"
        out_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": RESULTS_SCHEMA, "name": name, "run_id": args.run_id, "backend": pre,
            "sessions": [{"name": s.name, "role": s.role, "ai_pricing": s.ai_pricing} for s in sessions],
            "questions": [asdict(q) for q in questions], "results": [asdict(r) for r in results],
            "elapsed_s": round(time.time() - started, 1), "spent_usd": round(sum(r.cost_usd for r in results), 6),
        }
        (out_dir / "results.json").write_text(json.dumps(payload, indent=1, default=str))
        (out_dir / "table.md").write_text("\n".join(table) + "\n")
        for line in table:
            print(line)
        print(f"results: {out_dir / 'results.json'}")
        return 0 if all(r.status == "passed" for r in results) else 1
    except ChatCheckError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    finally:
        if e2e_env is not None:
            from scripts.plans_reader_live_run import _scramble

            try:
                _scramble(e2e_env)
                print("E2E password scrambled")
            except Exception as exc:  # noqa: BLE001 - report, never mask the result
                print(f"WARNING: E2E password scramble failed: {exc}", file=sys.stderr)


__all__ = [
    "Api", "ChatCheckError", "Expect", "Question", "QuestionResult", "Session", "ask", "default_questions",
    "describe_session", "format_questions", "format_table", "load_questions", "main", "parse_role_tokens",
    "pick_session", "preflight", "run_checks", "score", "tools_used",
]


if __name__ == "__main__":
    raise SystemExit(main())
