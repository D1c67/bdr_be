"""Auto send service (docs/AUTO_SEND.md sections 5 and 9).

Covers evaluate (every reason, the silent cases, the dev estimator, GCs
without email, bid contacts vs all contacts, the non-production block and
redirect), run_send (sent / partial / failed / unknown, the human-sent race,
the claim race, the manual re-send of a missed run, force_unknown),
reclaim_stuck, the markup amounts in the document, pending_project_ids,
accept_as_submitted (happy path, refusals, the 'auto_send_accept:<code>'
mapping) and the settings writes.

The Supabase client is the in-memory FakeDB of test_late_gc_pricing_approval,
extended with what this module reads (not_.in_, gte/lt, range, upsert, rpc
and the (project_id, bid_date) unique claim). Rendering is real (the docx
template ships with the app); the PDF converter, storage and Graph are fakes.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from types import SimpleNamespace

import httpx
import pytest
from postgrest.exceptions import APIError

from app.core.roles import AUTO_SEND_NOTIFY_ROLES, Role
from app.services import auto_send as asend
from app.services import office_preview, proposal_docx, proposal_send, storage, workflow
from app.services import graph_email
from app.services.auto_send import AutoSendError
from tests.test_late_gc_pricing_approval import FakeDB, _Query

NOW = datetime(2026, 10, 20, 0, 6, tzinfo=timezone.utc)  # 17:06 PT on 2026-10-19
BID_DAY = date(2026, 10, 19)
P1 = "p1"


# ── Fake Supabase ─────────────────────────────────────────────────────────────


class _NotProxy:
    def __init__(self, q):
        self.q = q

    def in_(self, col, vals):
        self.q._not_in.append((col, list(vals)))
        return self.q


class Query(_Query):
    def __init__(self, db, table):
        super().__init__(db, table)
        self._not_in = []
        self._ranges = []
        self._upsert = None

    @property
    def not_(self):
        return _NotProxy(self)

    def gte(self, col, val):
        self._ranges.append((col, ">=", val))
        return self

    def lt(self, col, val):
        self._ranges.append((col, "<", val))
        return self

    def range(self, lo, hi):
        return self

    def upsert(self, payload, on_conflict=None, ignore_duplicates=False):
        self._op, self._payload = "upsert", payload
        return self

    def _matches(self, row):
        if not super()._matches(row):
            return False
        if any(row.get(c) in vals for c, vals in self._not_in):
            return False
        for c, op, v in self._ranges:
            got = row.get(c)
            if got is None:
                return False
            a, b = _ts(got), _ts(v)
            if op == ">=" and not a >= b:
                return False
            if op == "<" and not a < b:
                return False
        return True

    def execute(self):
        rows = self.db.tables.setdefault(self.table, [])
        if self._op == "upsert":
            payloads = self._payload if isinstance(self._payload, list) else [self._payload]
            out = []
            for p in payloads:
                if not any(r.get("id") == p.get("id") for r in rows):
                    rows.append(dict(p))
                    out.append(dict(p))
            return SimpleNamespace(data=out)
        if self._op == "insert" and self.table == "auto_send_runs":
            payloads = self._payload if isinstance(self._payload, list) else [self._payload]
            for p in payloads:
                if any(r["project_id"] == p["project_id"] and r["bid_date"] == p["bid_date"]
                       for r in rows):
                    raise APIError({"message": "duplicate key value", "code": "23505",
                                    "hint": None, "details": None})
        if self.table in self.db.fail_tables and self._op in self.db.fail_tables[self.table]:
            raise RuntimeError(f"{self.table} {self._op} failed")
        return super().execute()


def _ts(v):
    if isinstance(v, datetime):
        return v
    return datetime.fromisoformat(str(v).replace("Z", "+00:00"))


class DB(FakeDB):
    def __init__(self, tables=None, rpc_error=None):
        super().__init__(tables)
        self.rpcs = []
        self.rpc_error = rpc_error
        self.fail_tables: dict[str, set] = {}

    def table(self, name):
        return Query(self, name)

    def rpc(self, name, params):
        self.rpcs.append((name, params))
        err = self.rpc_error

        class _Call:
            def execute(_self):
                if err:
                    raise err
                return SimpleNamespace(data={"ok": True})

        return _Call()


# ── fixtures ─────────────────────────────────────────────────────────────────

LINES = ["Furnish and install lighting per plans.", "Underground conduit for site lighting."]


def _estimate(**kw):
    out = {"ok": True, "code": None, "message": None, "file_id": "f-est",
           "filename": "Estimate.xlsx", "round": 1, "uploaded_by": "u-est",
           "estimator_name": "Eve Estimator", "components": {}, "materials": "1000.00",
           "labor": "2000.00", "base": "3000.00", "warnings": []}
    out.update(kw)
    return out


def _gc(gid, name, contacts, bid_contacts=()):
    return {"project_id": P1, "gc_id": gid,
            "general_contractors": {"id": gid, "name": name, "gc_contacts": contacts},
            "project_gc_contacts": [{"gc_contact_id": c} for c in bid_contacts],
            "pricing_approval_status": None, "proposal_material_amount": None,
            "proposal_gear_amount": None, "proposal_underground_amount": None,
            "proposal_low_voltage_amount": None, "proposal_labor_amount": None}


GC_A = _gc("gc-a", "Acme Builders",
           [{"id": "k-a1", "name": "Ann", "email": "ann@acme.test"},
            {"id": "k-a2", "name": "Al", "email": "al@acme.test"}],
           bid_contacts=["k-a1"])
GC_B = _gc("gc-b", "Brite Construction", [{"id": "k-b1", "name": "Bo", "email": "bo@brite.test"}])
GC_C = _gc("gc-c", "Cobalt General", [{"id": "k-c1", "name": "Cy", "email": None}])


def _lanes(intake=("to_estimator", "complete"), send_out=("gc_pricing", "locked")):
    heads = {"intake": intake, "material_numbers": ("receive_quotes", "active"),
             "labor_numbers": ("labor_numbers", "active"),
             "select_vendors": ("select_vendors", "active"), "markup": ("markup", "locked"),
             "send_out": send_out}
    return [{"project_id": P1, "category": c, "current_task": t, "status": s,
             "owner_role": None, "completed_at": None} for c, (t, s) in heads.items()]


def _db(*, project=None, gcs=(GC_A, GC_B), lanes=None, drafts=None, sends=(), runs=(),
        run_gcs=(), assignments=None, enabled=True, rpc_error=None, **extra):
    proj = {"id": P1, "name": "Sunset Plaza", "number": "26.10.7301",
            "address": "1 Main St, Las Vegas", "labor_time": "day_work",
            "wage_type": "prevailing_wage", "current_stage": "receive_quotes",
            "abandoned_at": None, "reverify_return_stage": None,
            "internal_bid_at": "2026-10-20T00:00:00+00:00",
            "created_at": "2026-10-01T00:00:00+00:00"}
    proj.update(project or {})
    tables = {
        "projects": [proj],
        "auto_send_settings": [{"id": 1, "enabled": enabled, "markup_pct": "10.00",
                                "enabled_changed_at": None, "updated_by": None,
                                "updated_at": None}],
        "project_category_state": lanes if lanes is not None else _lanes(),
        "project_gcs": [dict(g) for g in gcs],
        "proposal_drafts": drafts if drafts is not None else [
            {"id": "d1", "project_id": P1, "status": "done", "lines_json": list(LINES),
             "approved_at": None, "created_at": "2026-10-18T00:00:00+00:00"}],
        "proposal_sends": list(sends),
        "external_submissions": [],
        "bid_outcomes": [],
        "auto_send_runs": list(runs),
        "auto_send_gcs": list(run_gcs),
        "project_files": [],
        "estimator_assignments": assignments if assignments is not None else [
            {"project_id": P1, "estimator_id": "u-est", "revoked_at": None}],
        "profiles": [
            {"id": "u-est", "full_name": "Eve Estimator", "is_dev": False},
            {"id": "u-dev", "full_name": "Dev Account", "is_dev": True},
            {"id": "u-admin", "full_name": "Dana Admin", "is_dev": False},
        ],
    }
    tables.update(extra)
    return DB(tables, rpc_error=rpc_error)


def ref_apply_markup(materials, labor, pct):
    """The parser contract (section 4), for tests that run before the parser."""
    q = Decimal("0.01")
    factor = 1 + Decimal(pct) / 100
    m = (materials * factor).quantize(q, rounding=ROUND_HALF_UP)
    labor_out = (labor * factor).quantize(q, rounding=ROUND_HALF_UP)
    return m, labor_out, m + labor_out


SETTINGS = SimpleNamespace(is_production=True, auto_send_gc_redirect="",
                           auto_send_send_time="17:05", auto_send_preflight_time="15:00",
                           auto_send_catchup_minutes=120)


@pytest.fixture
def env(monkeypatch):
    class Env:
        def install(self, db, *, estimate=None, settings=SETTINGS, send=None):
            self.db = db
            self.mails, self.bells, self.audits, self.dismissals = [], [], [], []
            self.role_notes, self.uploads, self.previews = [], [], []
            self.estimate = estimate if estimate is not None else _estimate()
            for mod in (asend, proposal_send, workflow):
                monkeypatch.setattr(mod, "get_supabase", lambda: db)
            monkeypatch.setattr(asend, "get_settings", lambda: settings)
            monkeypatch.setattr(asend, "_now", lambda: NOW)
            monkeypatch.setattr(asend, "_pause", lambda: None)
            monkeypatch.setattr(asend, "load_estimate", lambda pid, sb=None: self.estimate)
            monkeypatch.setattr(asend, "apply_markup", ref_apply_markup)
            monkeypatch.setattr(asend, "_queue_preview", lambda fid: self.previews.append(fid))
            monkeypatch.setattr(
                asend, "notify_roles",
                lambda roles, pid, type_, msg, **kw: self.bells.append(
                    (frozenset(roles), pid, type_, msg, kw)),
            )
            monkeypatch.setattr(
                asend, "notify_role",
                lambda role, pid, type_, msg, **kw: self.role_notes.append((role, type_, msg)),
            )
            monkeypatch.setattr(
                asend, "audit",
                lambda actor, action, entity=None, entity_id=None, payload=None:
                self.audits.append((actor, action, entity, entity_id, payload)),
            )
            monkeypatch.setattr(asend, "dismiss_notifications",
                                lambda **kw: self.dismissals.append(kw))
            monkeypatch.setattr(workflow, "_dismiss_stale_notifications", lambda *a: None)
            monkeypatch.setattr(storage, "upload_file",
                                lambda path, content, ctype, **kw: self.uploads.append(path))
            # "PDF" = the docx bytes behind the magic; its text is the docx text.
            monkeypatch.setattr(office_preview, "convert_for_send",
                                lambda content, filename: b"%PDF-" + content)
            monkeypatch.setattr(
                office_preview, "extract_pdf_text",
                lambda pdf: proposal_docx.extract_document_text(pdf[5:]),
            )

            def _send_mail(**kw):
                self.mails.append(kw)
                if send is not None:
                    send(self, kw)
                return {"id": f"log-{len(self.mails)}"}

            monkeypatch.setattr(graph_email, "send_mail", _send_mail)
            return db

        def run(self):
            return self.db.tables["auto_send_runs"][-1]

        def gcs(self):
            return {r["gc_id"]: r for r in self.db.tables["auto_send_gcs"]}

    return Env()


def _codes(result):
    return [r["code"] for r in result["reasons"]]


# ── evaluate ────────────────────────────────────────────────────────────────


def test_evaluate_ready_with_markup_amounts_and_recipients(env):
    env.install(_db(gcs=(GC_A, GC_B, GC_C)))
    out = asend.evaluate(P1)
    assert out["outcome"] == "ready"
    assert out["bid_date"] == "2026-10-19"
    assert out["markup_pct"] == "10.00"
    assert out["amounts"] == {"material": "1100.00", "labor": "2200.00", "total": "3300.00",
                              "base_material": "1000.00", "base_labor": "2000.00"}
    assert out["draft"] == {"id": "d1", "lines": LINES, "approved": False}
    gcs = {g["gc_id"]: g for g in out["gcs"]}
    # Bid contacts win; otherwise every contact with an email.
    assert gcs["gc-a"]["recipients"] == ["ann@acme.test"]
    assert gcs["gc-a"]["source"] == "bid_contacts"
    assert gcs["gc-b"]["recipients"] == ["bo@brite.test"]
    assert gcs["gc-b"]["source"] == "all_contacts"
    # A GC with no usable email carries its own reason and does not block.
    assert gcs["gc-c"]["recipients"] == []
    assert gcs["gc-c"]["reason"]["code"] == "gc_no_email"
    assert "Cobalt General" in gcs["gc-c"]["reason"]["message"]


def test_stale_bid_contact_falls_back_to_all_contacts(env):
    stale = _gc("gc-a", "Acme Builders", GC_A["general_contractors"]["gc_contacts"],
                bid_contacts=["k-gone"])
    env.install(_db(gcs=(stale,)))
    gc = asend.evaluate(P1)["gcs"][0]
    assert gc["source"] == "all_contacts"
    assert gc["recipients"] == ["al@acme.test", "ann@acme.test"]


@pytest.mark.parametrize(
    "kw, silent",
    [
        ({"project": {"current_stage": "declined"}}, "stage"),
        ({"project": {"current_stage": "pm_only"}}, "stage"),
        ({"project": {"abandoned_at": "2026-10-10T00:00:00+00:00"}}, "abandoned"),
        ({"sends": [{"id": "ps1", "project_id": P1, "gc_id": "gc-a", "status": "sending"}]},
         "proposal_sent"),
        ({"sends": [{"id": "ps1", "project_id": P1, "gc_id": "gc-a", "status": "sent"}]},
         "proposal_sent"),
        ({"lanes": _lanes(send_out=("verify", "active")),
          "project": {"reverify_return_stage": "submitted"}}, "gone_out"),
        ({"runs": [{"id": "r0", "project_id": P1, "bid_date": "2026-10-12",
                    "status": "partial"}]}, "already_auto_sent"),
    ],
)
def test_evaluate_silent_cases(env, kw, silent):
    env.install(_db(**kw))
    out = asend.evaluate(P1)
    assert out["outcome"] == "silent"
    assert out["silent_reason"] == silent


def test_active_external_submission_is_silent(env):
    db = _db()
    db.tables["external_submissions"] = [{"id": "x1", "project_id": P1, "status": "active"}]
    env.install(db)
    assert asend.evaluate(P1)["silent_reason"] == "external_submission"


@pytest.mark.parametrize("who", ["uploader", "assignee"])
def test_dev_estimator_is_skipped(env, who):
    if who == "uploader":
        env.install(_db(), estimate=_estimate(uploaded_by="u-dev"))
    else:
        env.install(_db(assignments=[
            {"project_id": P1, "estimator_id": "u-est", "revoked_at": None},
            {"project_id": P1, "estimator_id": "u-dev", "revoked_at": None}]))
    out = asend.evaluate(P1)
    assert out["outcome"] == "skipped"
    assert _codes(out) == ["dev_estimator"]


def test_revoked_dev_assignment_does_not_skip(env):
    env.install(_db(assignments=[
        {"project_id": P1, "estimator_id": "u-est", "revoked_at": None},
        {"project_id": P1, "estimator_id": "u-dev", "revoked_at": "2026-10-01T00:00:00Z"}]))
    assert asend.evaluate(P1)["outcome"] == "ready"


@pytest.mark.parametrize(
    "kw, estimate, code",
    [
        ({"lanes": _lanes(intake=("go_no_go", "active"))}, None, "not_go"),
        ({"lanes": _lanes(intake=("intake", "active"))}, None, "not_go"),
        ({"assignments": []}, _estimate(ok=False, code="estimate_not_received", file_id=None,
                                        uploaded_by=None), "no_estimator"),
        ({}, _estimate(ok=False, code="estimate_not_received", file_id=None,
                       message="The estimator has not submitted an estimate yet."),
         "estimate_not_received"),
        ({}, _estimate(ok=False, code="crosscheck_failed", message="Totals do not add up."),
         "crosscheck_failed"),
        ({"drafts": []}, None, "no_scope_lines"),
        ({"drafts": [{"id": "d0", "project_id": P1, "status": "done", "lines_json": [],
                      "approved_at": None, "created_at": "2026-10-18T00:00:00Z"}]},
         None, "no_scope_lines"),
        ({"project": {"address": None}}, None, "missing_project_fields"),
        ({"project": {"name": "Sunset <Plaza>"}}, None, "bad_characters"),
        ({"gcs": ()}, None, "no_gcs"),
        ({"gcs": (GC_C,)}, None, "no_gc_emails"),
    ],
)
def test_evaluate_blocked_reasons(env, kw, estimate, code):
    env.install(_db(**kw), estimate=estimate)
    out = asend.evaluate(P1)
    assert out["outcome"] == "blocked"
    assert code in _codes(out)
    assert all(r["message"] for r in out["reasons"])


def test_estimate_failure_message_comes_from_the_estimate(env):
    env.install(_db(), estimate=_estimate(ok=False, code="unpriced_or_zero",
                                          message="The estimate is not priced."))
    out = asend.evaluate(P1)
    assert out["reasons"] == [{"code": "unpriced_or_zero",
                               "message": "The estimate is not priced."}]
    assert out["amounts"] is None


def test_newest_done_draft_with_lines_is_used_even_unapproved(env):
    drafts = [
        {"id": "d-old", "project_id": P1, "status": "done", "lines_json": ["Old line."],
         "approved_at": "2026-10-01T00:00:00Z", "created_at": "2026-10-01T00:00:00Z"},
        {"id": "d-new", "project_id": P1, "status": "done", "lines_json": ["New line."],
         "approved_at": None, "created_at": "2026-10-10T00:00:00Z"},
        {"id": "d-run", "project_id": P1, "status": "running", "lines_json": None,
         "approved_at": None, "created_at": "2026-10-11T00:00:00Z"},
    ]
    env.install(_db(drafts=drafts))
    assert asend.evaluate(P1)["draft"] == {"id": "d-new", "lines": ["New line."],
                                           "approved": False}


def test_non_production_without_redirect_blocks(env):
    env.install(_db(), settings=SimpleNamespace(**{**vars(SETTINGS), "is_production": False}))
    out = asend.evaluate(P1)
    assert out["outcome"] == "blocked"
    assert _codes(out) == ["gc_emails_blocked"]


def test_unknown_project_is_404(env):
    env.install(_db())
    with pytest.raises(AutoSendError) as exc:
        asend.evaluate("nope")
    assert exc.value.status_code == 404


# ── run_send ────────────────────────────────────────────────────────────────


def test_run_send_emails_every_gc_and_freezes_the_run(env):
    env.install(_db())
    out = asend.run_send(P1, BID_DAY)
    assert out["status"] == "sent"
    run = env.run()
    assert run["status"] == "sent" and run["trigger"] == "scheduled"
    assert run["material_amount"] == "1100.00" and run["total_amount"] == "3300.00"
    assert run["lines_json"] == LINES
    assert run["lines_hash"] == proposal_send.lines_hash(LINES)
    assert run["draft_id"] == "d1"
    assert run["sent_at"] and run["redirected_to"] is None
    assert len(env.mails) == 2
    by_to = {tuple(m["to"]): m for m in env.mails}
    acme = by_to[("ann@acme.test",)]
    assert acme["subject"] == "[G3 Electrical] Proposal: Sunset Plaza (26.10.7301)"
    assert acme["cc"] == proposal_send.cc_recipients(["ann@acme.test"])
    assert acme["project_id"] == P1 and acme["sent_by"] is None
    assert "Dear Acme Builders" in acme["body_html"]
    assert acme["attachments"][0][0] == "Proposal 7301 - Acme Builders.pdf"
    assert "\u2014" not in acme["subject"]
    gcs = env.gcs()
    assert {g["status"] for g in gcs.values()} == {"sent"}
    assert gcs["gc-a"]["recipients"] == "ann@acme.test"
    assert gcs["gc-a"]["email_log_id"] and gcs["gc-a"]["file_id"]
    files = env.db.tables["project_files"]
    assert {f["category"] for f in files} == {"proposal"}
    assert {f["gc_id"] for f in files} == {"gc-a", "gc-b"}
    assert all(f["uploaded_by"] is None for f in files)
    assert len(env.uploads) == 2 and len(env.previews) == 2
    # Auto send never writes the Send Out records.
    assert env.db.tables["proposal_sends"] == []
    assert "proposal_send_events" not in env.db.tables
    assert "stage_events" not in env.db.tables
    # One bell insert to the notify roles, bell only for a scheduled run.
    assert len(env.bells) == 1
    roles, _, type_, msg, kw = env.bells[0]
    assert roles == AUTO_SEND_NOTIFY_ROLES and type_ == "auto_send.sent"
    assert kw["mirror_email"] is False and kw["metadata"] == {"run_id": run["id"]}
    assert "\u2014" not in msg
    assert [a[1] for a in env.audits] == ["auto_send.sent"]


def test_document_carries_the_marked_up_amounts_and_no_section_rows(env):
    stored = []
    env.install(_db(gcs=(GC_A,)))
    real_store = asend._store_document

    def _spy(sb, pid, gid, filename, docx):
        stored.append(docx)
        return real_store(sb, pid, gid, filename, docx)

    asend._store_document = _spy
    try:
        asend.run_send(P1, BID_DAY)
    finally:
        asend._store_document = real_store
    text = proposal_docx.extract_document_text(stored[0])
    for figure in ("$1,100", "$2,200", "$3,300"):
        assert figure in text
    # The scope line may say Underground; the pricing table may not.
    assert "Underground conduit" in text
    asend.assert_pricing_table_clean(stored[0], "$3,300")


def test_pricing_table_check_refuses_a_section_row():
    template = proposal_docx.load_template_bytes()
    ctx = proposal_docx.ProposalContext(
        project_number="26.10.7301", project_name="Sunset", address="1 Main",
        gc_name="Acme", date_str="10/19/2026", labor_time="DAY", wage_text="Prevailing Wage",
        material_amount="$100", labor_amount="$200", total_amount="$350",
        underground_amount="$50", scope_lines=("Line one.",),
    )
    docx = proposal_docx.render_proposal(template, ctx)
    with pytest.raises(proposal_docx.ProposalRenderError):
        asend.assert_pricing_table_clean(docx, "$350")


def test_markup_rounds_each_part_and_total_is_their_sum(env):
    env.install(_db(gcs=(GC_A,)), estimate=_estimate(materials="1000.05", labor="0.05"))
    env.db.tables["auto_send_settings"][0]["markup_pct"] = "12.50"
    out = asend.evaluate(P1)
    assert out["markup_pct"] == "12.50"
    assert out["amounts"]["material"] == "1125.06"
    assert out["amounts"]["labor"] == "0.06"
    assert out["amounts"]["total"] == "1125.12"
    assert asend.run_send(P1, BID_DAY)["status"] == "sent"


def test_real_parser_apply_markup_when_available(env, monkeypatch):
    parser = pytest.importorskip("app.services.auto_send_estimate")
    env.install(_db(gcs=(GC_A,)))
    monkeypatch.setattr(asend, "apply_markup", parser.apply_markup)
    out = asend.evaluate(P1)
    assert out["amounts"]["total"] == "3300.00"


def test_claim_race_returns_none(env):
    env.install(_db(runs=[{"id": "r0", "project_id": P1, "bid_date": "2026-10-19",
                           "status": "sending"}]))
    assert asend.run_send(P1, BID_DAY) is None
    assert env.mails == []


def test_gc_without_email_is_a_failure_while_others_send(env):
    env.install(_db(gcs=(GC_A, GC_B, GC_C)))
    out = asend.run_send(P1, BID_DAY)
    assert out["status"] == "partial"
    gcs = env.gcs()
    assert gcs["gc-c"]["status"] == "failed"
    assert "Cobalt General has no contact" in gcs["gc-c"]["error"]
    assert {"code": "gc_no_email", "gc_id": "gc-c"}.items() <= out["reasons"][0].items()
    assert len(env.mails) == 2
    assert env.bells[0][2] == "auto_send.sent"


def test_send_failure_and_timeout(env):
    def _send(e, kw):
        if kw["to"] == ["ann@acme.test"]:
            raise httpx.ReadTimeout("slow")
        raise RuntimeError("mailbox full")

    env.install(_db(), send=_send)
    out = asend.run_send(P1, BID_DAY)
    assert out["status"] == "failed"
    gcs = env.gcs()
    assert gcs["gc-a"]["status"] == "unknown"
    assert "may or may not have gone out" in gcs["gc-a"]["error"]
    assert gcs["gc-b"]["status"] == "failed"
    assert "mailbox full" in gcs["gc-b"]["error"]
    assert sorted(_codes(out)) == ["outcome_unknown", "send_failed"]
    assert env.bells[0][2] == "auto_send.failed"
    assert [a[1] for a in env.audits] == ["auto_send.send_failed"]


def test_convert_failure_fails_the_gc(env, monkeypatch):
    env.install(_db(gcs=(GC_A,)))

    def _boom(content, filename):
        raise office_preview.ConversionError("down")

    monkeypatch.setattr(office_preview, "convert_for_send", _boom)
    out = asend.run_send(P1, BID_DAY)
    assert out["status"] == "failed"
    assert _codes(out) == ["convert_failed"]
    assert env.mails == []


def test_human_send_mid_run_stops_the_run(env):
    def _send(e, kw):
        # A person sends from Send Out right after the first auto email.
        e.db.tables["proposal_sends"].append(
            {"id": "ps1", "project_id": P1, "gc_id": "gc-b", "status": "sending"})

    env.install(_db(), send=_send)
    out = asend.run_send(P1, BID_DAY)
    assert len(env.mails) == 1
    assert out["status"] == "partial"
    gcs = env.gcs()
    assert gcs["gc-a"]["status"] == "sent"
    assert gcs["gc-b"]["status"] == "skipped"
    assert "human_sent" in _codes(out)


def test_blocked_scheduled_run_is_recorded_failed(env):
    env.install(_db(drafts=[]))
    out = asend.run_send(P1, BID_DAY)
    assert out["status"] == "failed"
    assert _codes(out) == ["no_scope_lines"]
    assert env.mails == []
    assert env.bells[0][2] == "auto_send.failed"


def test_dev_estimator_run_is_skipped_without_a_bell(env):
    env.install(_db(), estimate=_estimate(uploaded_by="u-dev"))
    out = asend.run_send(P1, BID_DAY)
    assert out["status"] == "skipped"
    assert env.mails == [] and env.bells == []
    assert [a[1] for a in env.audits] == ["auto_send.skipped"]


def test_redirect_outside_production(env):
    settings = SimpleNamespace(**{**vars(SETTINGS), "is_production": False,
                                  "auto_send_gc_redirect": "qa@g3.test"})
    env.install(_db(gcs=(GC_A,)), settings=settings)
    out = asend.run_send(P1, BID_DAY)
    assert out["status"] == "sent"
    mail = env.mails[0]
    assert mail["to"] == ["qa@g3.test"] and mail["cc"] == []
    assert mail["subject"] == ("[AUTO SEND TEST to ann@acme.test] "
                               "[G3 Electrical] Proposal: Sunset Plaza (26.10.7301)")
    assert env.run()["redirected_to"] == "qa@g3.test"
    assert env.gcs()["gc-a"]["recipients"] == "ann@acme.test"


def test_unexpected_crash_never_strands_the_claim(env, monkeypatch):
    env.install(_db())
    monkeypatch.setattr(proposal_docx, "load_template_bytes",
                        lambda: (_ for _ in ()).throw(RuntimeError("disk")))
    out = asend.run_send(P1, BID_DAY)
    assert out["status"] == "failed"
    assert _codes(out)[0] == "interrupted"


# ── manual send now ─────────────────────────────────────────────────────────


def test_send_now_on_missed_run(env):
    env.install(_db(runs=[{"id": "r1", "project_id": P1, "bid_date": "2026-10-19",
                           "status": "missed", "trigger": "scheduled",
                           "created_at": "2026-10-20T03:00:00Z"}]))
    out = asend.send_now(P1, "u-admin")
    assert out["status"] == "sent"
    run = env.run()
    assert run["trigger"] == "manual" and run["triggered_by"] == "u-admin"
    assert all(m["sent_by"] == "u-admin" for m in env.mails)
    assert env.bells[0][4]["mirror_email"] is True
    assert {"project_id": P1, "types": ["auto_send.missed"]} in env.dismissals


def test_send_now_on_partial_only_resends_unsent_gcs(env):
    run = {"id": "r1", "project_id": P1, "bid_date": "2026-10-19", "status": "partial",
           "material_amount": "1100.00", "labor_amount": "2200.00", "total_amount": "3300.00",
           "lines_json": list(LINES), "lines_hash": proposal_send.lines_hash(LINES),
           "sent_at": "2026-10-20T00:05:30Z", "created_at": "2026-10-20T00:05:00Z"}
    rows = [
        {"id": "g1", "run_id": "r1", "project_id": P1, "gc_id": "gc-a",
         "gc_name": "Acme Builders", "status": "sent", "attempts": 1},
        {"id": "g2", "run_id": "r1", "project_id": P1, "gc_id": "gc-b",
         "gc_name": "Brite Construction", "status": "failed", "attempts": 1,
         "error": "The email to Brite Construction failed: x"},
    ]
    env.install(_db(runs=[run], run_gcs=rows), estimate=_estimate(materials="5.00"))
    out = asend.send_now(P1, "u-admin")
    assert [m["to"] for m in env.mails] == [["bo@brite.test"]]
    assert out["status"] == "sent"
    # The partly sent run keeps the numbers it already sent.
    assert env.gcs()["gc-b"]["material_amount"] == "1100.00"
    assert env.gcs()["gc-b"]["attempts"] == 2


def test_unknown_gcs_need_force(env):
    run = {"id": "r1", "project_id": P1, "bid_date": "2026-10-19", "status": "failed",
           "created_at": "2026-10-20T00:05:00Z"}
    rows = [{"id": "g1", "run_id": "r1", "project_id": P1, "gc_id": "gc-a",
             "gc_name": "Acme Builders", "status": "unknown", "attempts": 1,
             "error": "timed out"}]
    env.install(_db(gcs=(GC_A,), runs=[run], run_gcs=rows))
    with pytest.raises(AutoSendError) as exc:
        asend.send_now(P1, "u-admin")
    assert exc.value.code == "unknown_needs_force"
    assert env.mails == []
    out = asend.send_now(P1, "u-admin", force_unknown=True)
    assert out["status"] == "sent" and len(env.mails) == 1


@pytest.mark.parametrize(
    "runs, enabled, code",
    [
        ([], True, "nothing_to_send"),
        ([{"id": "r1", "project_id": P1, "bid_date": "2026-10-19", "status": "sent"}],
         True, "nothing_to_send"),
        ([{"id": "r1", "project_id": P1, "bid_date": "2026-10-19", "status": "sending"}],
         True, "busy"),
        ([{"id": "r1", "project_id": P1, "bid_date": "2026-10-19", "status": "missed"}],
         False, "disabled"),
    ],
)
def test_send_now_refusals(env, runs, enabled, code):
    env.install(_db(runs=runs, enabled=enabled))
    with pytest.raises(AutoSendError) as exc:
        asend.send_now(P1, "u-admin")
    assert exc.value.status_code == 409 and exc.value.code == code


def test_send_now_ignores_a_gc_that_failed_only_for_having_no_email(env):
    # A partial run whose only unsent GC has no email: nothing is left to send,
    # so no button and no repeat "could not send" alert for every notify role.
    run = {"id": "r1", "project_id": P1, "bid_date": "2026-10-19", "status": "partial",
           "material_amount": "1100.00", "labor_amount": "2200.00", "total_amount": "3300.00",
           "lines_json": list(LINES), "lines_hash": proposal_send.lines_hash(LINES),
           "sent_at": "2026-10-20T00:05:30Z", "created_at": "2026-10-20T00:05:00Z"}
    rows = [
        {"id": "g1", "run_id": "r1", "project_id": P1, "gc_id": "gc-a",
         "gc_name": "Acme Builders", "status": "sent", "attempts": 1},
        {"id": "g3", "run_id": "r1", "project_id": P1, "gc_id": "gc-c",
         "gc_name": "Cobalt General", "status": "failed", "attempts": 0,
         "error": "Cobalt General has no contact with an email address."},
    ]
    env.install(_db(gcs=(GC_A, GC_C), runs=[run], run_gcs=rows))
    with pytest.raises(AutoSendError) as exc:
        asend.send_now(P1, "u-admin")
    assert exc.value.code == "nothing_to_send"
    assert env.mails == [] and env.bells == []


def test_send_now_refuses_an_old_run_after_the_date_moved_later(env):
    env.install(_db(project={"internal_bid_at": "2026-10-23T00:00:00+00:00"},
                    runs=[{"id": "r1", "project_id": P1, "bid_date": "2026-10-19",
                           "status": "missed", "trigger": "scheduled",
                           "created_at": "2026-10-20T03:00:00Z"}]))
    with pytest.raises(AutoSendError) as exc:
        asend.send_now(P1, "u-admin")
    assert exc.value.code == "date_moved"
    assert env.mails == []


def test_manual_run_send_requires_a_sendable_run(env):
    env.install(_db())
    with pytest.raises(AutoSendError):
        asend.run_send(P1, BID_DAY, trigger="manual", actor_id="u-admin")


# ── missed / reclaim ────────────────────────────────────────────────────────


def test_record_missed_once(env):
    env.install(_db())
    row = asend.record_missed(P1, BID_DAY, None)
    assert row["status"] == "missed"
    assert row["reasons"][0]["code"] == "missed"
    assert asend.record_missed(P1, BID_DAY, None) is None


def test_reclaim_stuck(env):
    old = (NOW - timedelta(minutes=20)).isoformat()
    fresh = (NOW - timedelta(minutes=2)).isoformat()
    runs = [
        {"id": "r1", "project_id": P1, "bid_date": "2026-10-19", "status": "sending",
         "started_at": old},
        {"id": "r2", "project_id": "p2", "bid_date": "2026-10-19", "status": "sending",
         "started_at": fresh},
    ]
    rows = [
        {"id": "g1", "run_id": "r1", "project_id": P1, "gc_id": "gc-a",
         "gc_name": "Acme Builders", "status": "sent"},
        {"id": "g2", "run_id": "r1", "project_id": P1, "gc_id": "gc-b",
         "gc_name": "Brite Construction", "status": "sending"},
    ]
    env.install(_db(runs=runs, run_gcs=rows))
    assert asend.reclaim_stuck() == 1
    r1, r2 = env.db.tables["auto_send_runs"]
    assert r1["status"] == "partial" and r1["reasons"][0]["code"] == "outcome_unknown"
    assert r2["status"] == "sending"
    assert env.gcs()["gc-b"]["status"] == "unknown"
    assert asend.reclaim_stuck() == 0


# ── red pulse / revised ─────────────────────────────────────────────────────


def test_pending_project_ids(env):
    runs = [
        {"id": "r1", "project_id": "p1", "bid_date": "2026-10-19", "status": "sent",
         "accepted_at": None},
        {"id": "r2", "project_id": "p2", "bid_date": "2026-10-19", "status": "partial",
         "accepted_at": None},
        {"id": "r3", "project_id": "p3", "bid_date": "2026-10-19", "status": "sent",
         "accepted_at": "2026-10-20T01:00:00Z"},
        {"id": "r4", "project_id": "p4", "bid_date": "2026-10-19", "status": "failed",
         "accepted_at": None},
        {"id": "r5", "project_id": "p5", "bid_date": "2026-10-19", "status": "sent",
         "accepted_at": None},
    ]
    sends = [{"id": "ps", "project_id": "p5", "gc_id": "gc-a", "status": "sent"}]
    env.install(_db(runs=runs, sends=sends))
    assert asend.pending_project_ids(["p1", "p2", "p3", "p4", "p5", "p9"]) == {"p1", "p2"}
    assert asend.pending_project_ids([]) == set()


def test_auto_sent_gc_ids(env):
    rows = [
        {"id": "g1", "run_id": "r1", "project_id": P1, "gc_id": "gc-a", "status": "sent"},
        {"id": "g2", "run_id": "r1", "project_id": P1, "gc_id": "gc-b", "status": "failed"},
    ]
    env.install(_db(run_gcs=rows))
    assert asend.auto_sent_gc_ids(P1) == {"gc-a"}


def test_due_projects_filters_by_pacific_day(env):
    db = _db()
    db.tables["projects"] += [
        {"id": "p-late", "internal_bid_at": "2026-10-20T07:30:00+00:00",  # 00:30 PT 10-20
         "current_stage": "rfqs", "abandoned_at": None},
        {"id": "p-early", "internal_bid_at": "2026-10-19T07:30:00+00:00",  # 00:30 PT 10-19
         "current_stage": "rfqs", "abandoned_at": None},
        {"id": "p-sub", "internal_bid_at": "2026-10-19T20:00:00+00:00",
         "current_stage": "submitted", "abandoned_at": None},
    ]
    env.install(db)
    assert {p["id"] for p in asend.due_projects(BID_DAY)} == {P1, "p-early"}


# ── accept as submitted ─────────────────────────────────────────────────────


def _sent_run(**kw):
    run = {"id": "r1", "project_id": P1, "bid_date": "2026-10-19", "status": "sent",
           "accepted_at": None, "sent_at": "2026-10-20T00:05:30Z", "total_amount": "3300.00",
           "created_at": "2026-10-20T00:05:00Z"}
    run.update(kw)
    return run


SENT_ROWS = [
    {"id": "g1", "run_id": "r1", "project_id": P1, "gc_id": "gc-a",
     "gc_name": "Acme Builders", "status": "sent"},
    {"id": "g2", "run_id": "r1", "project_id": P1, "gc_id": "gc-b",
     "gc_name": "Brite Construction", "status": "failed", "error": "x"},
]


def test_accept_happy_path(env):
    env.install(_db(runs=[_sent_run()], run_gcs=SENT_ROWS))
    out = asend.accept_as_submitted(P1, "r1", "u-admin")
    assert out["gcs"] == ["Acme Builders"]
    name, params = env.db.rpcs[0]
    assert name == "apply_auto_send_acceptance"
    p = params["p"]
    assert p["project_id"] == P1 and p["run_id"] == "r1" and p["actor_id"] == "u-admin"
    assert p["expected_send_out_head"] == "gc_pricing"
    lanes = {r["category"]: r for r in p["lanes"]}
    assert lanes["send_out"] == {**lanes["send_out"], "current_task": "submitted",
                                 "status": "active"}
    assert all(r["status"] == "complete" for c, r in lanes.items() if c != "send_out")
    assert p["events"][-1]["to_stage"] == "submitted"
    assert "Dana Admin" in p["note"] and "Acme Builders" in p["note"]
    assert "\u2014" not in p["note"]
    assert [a[1] for a in env.audits] == ["auto_send.accepted"]
    assert {"project_id": P1, "types": ["proposal_send_failed"],
            "type_prefixes": ["auto_send."]} in env.dismissals
    assert {n[0] for n in env.role_notes} == {
        Role.ESTIMATING_ENGINEER_MATERIALS, Role.ESTIMATING_ENGINEER_LABOR, Role.EXECUTIVE}
    assert all(n[1] == "submitted" for n in env.role_notes)


@pytest.mark.parametrize(
    "kw, code, status_code",
    [
        ({"runs": [_sent_run(accepted_at="2026-10-20T01:00:00Z")]}, "already_accepted", 409),
        ({"runs": [_sent_run(status="failed")]}, "run_not_sent", 409),
        ({"runs": [_sent_run(project_id="p2")]}, "run_not_for_project", 404),
        ({"runs": [_sent_run()],
          "sends": [{"id": "ps", "project_id": P1, "gc_id": "gc-a", "status": "sent"}]},
         "already_sent", 409),
        ({"runs": [_sent_run()], "bid_outcomes": [{"id": "o1", "project_id": P1}]},
         "outcome_recorded", 409),
    ],
)
def test_accept_refusals(env, kw, code, status_code):
    env.install(_db(run_gcs=SENT_ROWS, **kw))
    with pytest.raises(AutoSendError) as exc:
        asend.accept_as_submitted(P1, "r1", "u-admin")
    assert exc.value.code == code and exc.value.status_code == status_code
    assert env.db.rpcs == []


@pytest.mark.parametrize(
    "db_code, expect",
    [("stale", "stale"), ("already_accepted", "already_accepted"),
     ("already_sent", "already_sent"), ("gc_already_sent", "gc_already_sent"),
     ("external_submission_active", "external_submission_active"),
     ("run_not_sent", "run_not_sent"), ("no_sent_gcs", "no_sent_gcs")],
)
def test_accept_maps_database_codes_to_409(env, db_code, expect):
    err = APIError({"message": f"auto_send_accept:{db_code}", "code": "P0001",
                    "hint": None, "details": None})
    env.install(_db(runs=[_sent_run()], run_gcs=SENT_ROWS, rpc_error=err))
    with pytest.raises(AutoSendError) as exc:
        asend.accept_as_submitted(P1, "r1", "u-admin")
    assert exc.value.status_code == 409 and exc.value.code == expect
    assert str(exc.value) == asend.ACTION_MESSAGES[expect]
    assert env.audits == []


def test_accept_uncoded_database_error_is_500_with_a_message(env):
    err = APIError({"message": "numeric field overflow", "code": "22003",
                    "hint": None, "details": None})
    env.install(_db(runs=[_sent_run()], run_gcs=SENT_ROWS, rpc_error=err))
    with pytest.raises(AutoSendError) as exc:
        asend.accept_as_submitted(P1, "r1", "u-admin")
    assert exc.value.status_code == 500 and str(exc.value)


def test_accept_error_code_parsing():
    assert asend.accept_error_code(Exception("ERROR: auto_send_accept:stale (P0001)")) == "stale"
    assert asend.accept_error_code(Exception("boom")) is None


# ── settings ────────────────────────────────────────────────────────────────


def test_set_enabled_stamps_the_flip_and_audits_with_null_entity(env):
    db = env.install(_db(enabled=False))
    row = asend.set_enabled(True, "u-it")
    assert row["enabled"] is True
    assert row["enabled_changed_at"] == NOW.isoformat()
    actor, action, entity, entity_id, payload = env.audits[-1]
    assert (actor, action, entity, entity_id) == (
        "u-it", "auto_send.enabled_changed", "auto_send_settings", None)
    assert payload == {"id": 1, "from": False, "to": True}
    # No flip, no new stamp.
    db.tables["auto_send_settings"][0]["enabled_changed_at"] = "earlier"
    asend.set_enabled(True, "u-it")
    assert db.tables["auto_send_settings"][0]["enabled_changed_at"] == "earlier"


@pytest.mark.parametrize("bad", ["-1", "100.01", "abc", "NaN", "1e30"])
def test_set_markup_pct_validation(env, bad):
    env.install(_db())
    with pytest.raises(ValueError):
        asend.set_markup_pct(bad, "u-exec")
    assert env.audits == []


def test_set_markup_pct_rounds_and_audits(env):
    env.install(_db())
    row = asend.set_markup_pct(Decimal("12.345"), "u-exec")
    assert row["markup_pct"] == "12.35"
    assert env.audits[-1][1:4] == ("auto_send.markup_changed", "auto_send_settings", None)
    assert env.audits[-1][4] == {"id": 1, "from": "10.00", "to": "12.35"}


def test_settings_row_self_heals(env):
    db = env.install(_db())
    db.tables["auto_send_settings"] = []
    row = asend.get_settings_row()
    assert row["id"] == 1
    assert db.tables["auto_send_settings"] == [{"id": 1}]


def test_settings_read_failure_raises(env):
    db = env.install(_db())
    db.fail_tables["auto_send_settings"] = {"select"}
    with pytest.raises(RuntimeError):
        asend.get_settings_row()


# ── panel ───────────────────────────────────────────────────────────────────


def _user(role):
    return SimpleNamespace(id="u-admin", role=role)


def test_project_summary_after_a_partial_send(env):
    run = _sent_run(status="partial", accepted_by=None, triggered_by="u-admin",
                    lines_json=list(LINES))
    rows = [dict(r, file_id="f1" if r["gc_id"] == "gc-a" else None) for r in SENT_ROWS]
    db = _db(runs=[run], run_gcs=rows)
    db.tables["project_files"] = [{"id": "f1", "filename": "Proposal 7301 - Acme Builders.docx"}]
    env.install(db)
    out = asend.project_summary(P1, _user(Role.ESTIMATING_ADMIN))
    assert out["pending"] is True
    assert out["preview"] is None  # a run already went out
    assert out["can_accept"] is True and out["can_send_now"] is True
    latest = out["latest_run"]
    assert latest["lines"] == LINES and latest["triggered_by_name"] == "Dana Admin"
    gcs = {g["gc_id"]: g for g in latest["gcs"]}
    assert gcs["gc-a"]["filename"] == "Proposal 7301 - Acme Builders.docx"
    assert out["lines_review"] == {"draft_id": "d1", "status": "done", "approved": False,
                                   "lines_count": 2}
    # The accountant reads it all and acts on nothing.
    acct = asend.project_summary(P1, _user(Role.ACCOUNTANT))
    assert acct["can_accept"] is False and acct["can_send_now"] is False


def test_project_summary_before_any_run_has_a_preview(env):
    env.install(_db())
    out = asend.project_summary(P1, _user(Role.EXECUTIVE))
    assert out["latest_run"] is None and out["pending"] is False
    assert out["preview"]["outcome"] == "ready"
    assert out["can_send_now"] is False and out["can_accept"] is False
    assert out["send_time"] == "17:05" and out["bid_date"] == "2026-10-19"


def test_project_summary_missed_run_can_send_now(env):
    env.install(_db(runs=[{"id": "r1", "project_id": P1, "bid_date": "2026-10-19",
                           "status": "missed"}]))
    assert asend.project_summary(P1, _user(Role.IT_ADMIN))["can_send_now"] is True
    env.db.tables["auto_send_settings"][0]["enabled"] = False
    assert asend.project_summary(P1, _user(Role.IT_ADMIN))["can_send_now"] is False


def test_no_em_dashes_in_messages():
    for table in (asend.REASON_MESSAGES, asend.SILENT_MESSAGES, asend.ACTION_MESSAGES):
        assert all("\u2014" not in m for m in table.values())


# ── review fixes (2026-10-19) ───────────────────────────────────────────────


# Finding 1: a dev "View as" estimate is dev-priced.


def test_impersonated_workbook_is_skipped_as_dev_estimator(env):
    env.install(_db(), estimate=_estimate(impersonated_by="u-dev"))
    out = asend.evaluate(P1)
    assert out["outcome"] == "skipped" and _codes(out) == ["dev_estimator"]
    assert asend.run_send(P1, BID_DAY)["status"] == "skipped"
    assert env.mails == []


def test_impersonated_round_is_skipped_as_dev_estimator(env):
    env.install(_db(estimator_submissions=[
        {"id": "s1", "project_id": P1, "round": 1, "impersonated_by": "u-dev"}]))
    assert asend.evaluate(P1)["outcome"] == "skipped"


def test_impersonation_audit_trail_covers_rounds_sealed_before_the_stamp(env):
    est = _estimate(uploaded_at="2026-10-18T10:00:00+00:00")
    audit_rows = [{"id": "a1", "action": "impersonation.write", "entity_id": "u-est",
                   "actor_id": "u-dev", "created_at": "2026-10-18T10:05:00+00:00",
                   "payload": {"method": "POST", "path": f"/estimator/projects/{P1}/submit"}}]
    env.install(_db(audit_log=audit_rows), estimate=est)
    assert asend.evaluate(P1)["outcome"] == "skipped"


@pytest.mark.parametrize("row", [
    # Another project.
    {"id": "a1", "action": "impersonation.write", "entity_id": "u-est",
     "created_at": "2026-10-18T10:05:00+00:00",
     "payload": {"method": "POST", "path": "/estimator/projects/p9/submit"}},
    # Long before this workbook was uploaded (an older round).
    {"id": "a2", "action": "impersonation.write", "entity_id": "u-est",
     "created_at": "2026-09-01T10:05:00+00:00",
     "payload": {"method": "POST", "path": f"/projects/{P1}/files"}},
])
def test_unrelated_impersonation_does_not_block(env, row):
    env.install(_db(audit_log=[row]), estimate=_estimate(uploaded_at="2026-10-18T10:00:00+00:00"))
    assert asend.evaluate(P1)["outcome"] == "ready"


def test_normal_estimator_submit_is_still_ready(env):
    env.install(_db(estimator_submissions=[
        {"id": "s1", "project_id": P1, "round": 1, "impersonated_by": None}]))
    assert asend.evaluate(P1)["outcome"] == "ready"


# Finding 2: a malformed id is a 404, never a raw 500.


class _BadUuidDB(DB):
    def table(self, name):
        q = super().table(name)
        real = q.execute

        def _execute():
            ids = [v for c, v in q._filters if c == "id"]
            if name in ("projects", "auto_send_runs") and any(v == "not-a-uuid" for v in ids):
                raise APIError({"message": "invalid input syntax for type uuid",
                                "code": "22P02", "hint": None, "details": None})
            return real()

        q.execute = _execute
        return q


def test_malformed_project_id_is_not_found(env):
    db = _db()
    env.install(_BadUuidDB(db.tables))
    for call in (lambda: asend.project_summary("not-a-uuid", SimpleNamespace(role="executive")),
                 lambda: asend.send_now("not-a-uuid", "u-admin"),
                 lambda: asend.accept_as_submitted("not-a-uuid", "r1", "u-admin")):
        with pytest.raises(AutoSendError) as exc:
            call()
        assert exc.value.status_code == 404


def test_malformed_run_id_is_not_found(env):
    db = _db(runs=[_sent_run()], run_gcs=SENT_ROWS)
    env.install(_BadUuidDB(db.tables))
    with pytest.raises(AutoSendError) as exc:
        asend.accept_as_submitted(P1, "not-a-uuid", "u-admin")
    assert exc.value.status_code == 404 and exc.value.code == "run_not_for_project"


# Findings 4 / 15: an earlier run's timed-out GC is never emailed again
# automatically after the internal date moves.


def _earlier_failed_run():
    run = {"id": "r-old", "project_id": P1, "bid_date": "2026-10-12", "status": "failed",
           "created_at": "2026-10-13T00:05:00Z"}
    rows = [
        {"id": "o1", "run_id": "r-old", "project_id": P1, "gc_id": "gc-a",
         "gc_name": "Acme Builders", "status": "unknown", "error": "timed out"},
        {"id": "o2", "run_id": "r-old", "project_id": P1, "gc_id": "gc-b",
         "gc_name": "Brite Construction", "status": "failed", "error": "mailbox full"},
    ]
    return run, rows


def test_earlier_unknown_gc_is_carried_as_unknown_into_the_new_date(env):
    run, rows = _earlier_failed_run()
    env.install(_db(runs=[run], run_gcs=rows))
    out = asend.run_send(P1, BID_DAY)
    assert [m["to"] for m in env.mails] == [["bo@brite.test"]]
    new = {r["gc_id"]: r for r in env.db.tables["auto_send_gcs"] if r["run_id"] == out["id"]}
    assert new["gc-a"]["status"] == "unknown"
    assert "may already have gone out" in new["gc-a"]["error"]
    assert out["status"] == "partial"
    assert "earlier_outcome_unknown" in _codes(out)


def test_carried_unknown_gc_needs_a_person_to_force_it(env):
    run, rows = _earlier_failed_run()
    rows = [r for r in rows if r["gc_id"] == "gc-a"]
    env.install(_db(gcs=(GC_A,), runs=[run], run_gcs=rows))
    out = asend.run_send(P1, BID_DAY)
    assert out["status"] == "failed" and env.mails == []
    with pytest.raises(AutoSendError) as exc:
        asend.send_now(P1, "u-admin")
    assert exc.value.code == "unknown_needs_force"
    out = asend.send_now(P1, "u-admin", force_unknown=True)
    assert out["status"] == "sent" and len(env.mails) == 1


def test_send_now_is_busy_while_any_run_of_the_project_is_sending(env):
    runs = [
        {"id": "r-old", "project_id": P1, "bid_date": "2026-10-12", "status": "sending",
         "created_at": "2026-10-13T00:05:00Z"},
        {"id": "r1", "project_id": P1, "bid_date": "2026-10-19", "status": "failed",
         "created_at": "2026-10-20T00:05:00Z"},
    ]
    env.install(_db(runs=runs))
    with pytest.raises(AutoSendError) as exc:
        asend.send_now(P1, "u-admin")
    assert exc.value.code == "busy"


# Findings 5 / 18: the human-send check runs again right before send_mail.


def test_human_send_during_the_pdf_conversion_wins(env, monkeypatch):
    env.install(_db(gcs=(GC_A,)))

    def _slow_convert(content, filename):
        # A PA clicks Send Out while the converter works.
        env.db.tables["proposal_sends"].append(
            {"id": "ps1", "project_id": P1, "gc_id": "gc-a", "status": "sending"})
        return b"%PDF-" + content

    monkeypatch.setattr(office_preview, "convert_for_send", _slow_convert)
    out = asend.run_send(P1, BID_DAY)
    assert env.mails == []
    assert env.gcs()["gc-a"]["status"] == "skipped"
    assert "human_sent" in _codes(out)


# Finding 6: a Send Out that timed out blocks auto send.


TIMED_OUT_SEND = {"id": "ps1", "project_id": P1, "gc_id": "gc-a", "status": "failed",
                  "error": f"{proposal_send.OUTCOME_UNKNOWN_PREFIX} (ReadTimeout) verify"}


def test_human_send_with_unknown_outcome_blocks(env):
    env.install(_db(sends=[TIMED_OUT_SEND]))
    out = asend.evaluate(P1)
    assert out["outcome"] == "blocked" and "human_send_unknown" in _codes(out)
    assert asend.run_send(P1, BID_DAY)["status"] == "failed"
    assert env.mails == []


def test_plain_failed_human_send_does_not_block(env):
    env.install(_db(sends=[{**TIMED_OUT_SEND, "error": "mailbox full"}]))
    assert asend.evaluate(P1)["outcome"] == "ready"


# Findings 7 / 14: turning the switch off stops the run at the next GC.


def test_switch_off_mid_run_stops_the_remaining_gcs(env):
    def _send(e, kw):
        e.db.tables["auto_send_settings"][0]["enabled"] = False

    env.install(_db(), send=_send)
    out = asend.run_send(P1, BID_DAY)
    assert len(env.mails) == 1
    assert out["status"] == "partial"
    gcs = env.gcs()
    assert gcs["gc-b"]["status"] == "skipped"
    assert "turned off" in gcs["gc-b"]["error"]
    assert "switched_off" in _codes(out)


def test_switch_read_failure_counts_as_off(env):
    env.install(_db())
    env.db.fail_tables["auto_send_settings"] = {"select"}
    assert asend._switched_off(env.db) is True


# Finding 9: a scheduled run finishing after the digest went out emails.


@pytest.mark.parametrize("digest_out", [False, True])
def test_scheduled_bell_mirrors_once_the_digest_went_out(env, digest_out):
    log = ([{"id": "l1", "send_date": "2026-10-19", "kind": "digest", "user_id": "u-admin"}]
           if digest_out else [])
    env.install(_db(gcs=(GC_A,), auto_send_email_log=log))
    asend.run_send(P1, BID_DAY)
    assert env.bells[0][4]["mirror_email"] is digest_out


# Findings 10 / 17: reclaim measures the heartbeat, a reclaimed worker stops.


def test_reclaim_leaves_a_slow_run_that_still_beats(env):
    old = (NOW - timedelta(minutes=40)).isoformat()
    beat = (NOW - timedelta(minutes=3)).isoformat()
    runs = [{"id": "r1", "project_id": P1, "bid_date": "2026-10-19", "status": "sending",
             "started_at": old, "updated_at": beat}]
    rows = [{"id": "g1", "run_id": "r1", "project_id": P1, "gc_id": "gc-a",
             "gc_name": "Acme Builders", "status": "sending", "updated_at": old}]
    env.install(_db(runs=runs, run_gcs=rows))
    assert asend.reclaim_stuck() == 0
    assert env.gcs()["gc-a"]["status"] == "sending"


def test_reclaim_counts_gc_row_activity_as_a_heartbeat(env):
    old = (NOW - timedelta(minutes=40)).isoformat()
    runs = [{"id": "r1", "project_id": P1, "bid_date": "2026-10-19", "status": "sending",
             "started_at": old, "updated_at": old}]
    rows = [{"id": "g1", "run_id": "r1", "project_id": P1, "gc_id": "gc-a",
             "gc_name": "Acme Builders", "status": "sending",
             "updated_at": (NOW - timedelta(minutes=1)).isoformat()}]
    env.install(_db(runs=runs, run_gcs=rows))
    assert asend.reclaim_stuck() == 0


def test_stuck_minutes_follows_the_converter_timeout(env, monkeypatch):
    env.install(_db())
    assert asend._stuck_minutes() == asend.STUCK_MINUTES
    slow = SimpleNamespace(**{**vars(SETTINGS), "preview_convert_timeout_seconds": 600})
    monkeypatch.setattr(asend, "get_settings", lambda: slow)
    assert asend._stuck_minutes() > 40


def test_a_reclaimed_worker_sends_nothing_more(env):
    def _send(e, kw):
        # Another worker reclaimed the run while this one was mid-send.
        e.db.tables["auto_send_runs"][-1]["status"] = "failed"

    env.install(_db(), send=_send)
    out = asend.run_send(P1, BID_DAY)
    assert len(env.mails) == 1
    assert out["status"] == "failed"  # the reclaimer's record is not overwritten
    assert env.bells == []
    assert "auto_send.run_lost" in [a[1] for a in env.audits]


# Finding 12: crashed and reclaimed runs still ring the bell.


def test_crashed_run_still_rings_the_bell(env, monkeypatch):
    env.install(_db())
    monkeypatch.setattr(proposal_docx, "load_template_bytes",
                        lambda: (_ for _ in ()).throw(RuntimeError("disk")))
    asend.run_send(P1, BID_DAY)
    assert [b[2] for b in env.bells] == ["auto_send.failed"]


def test_reclaimed_manual_run_rings_and_mirrors(env):
    old = (NOW - timedelta(minutes=20)).isoformat()
    runs = [{"id": "r1", "project_id": P1, "bid_date": "2026-10-19", "status": "sending",
             "trigger": "manual", "triggered_by": "u-admin", "started_at": old}]
    rows = [
        {"id": "g1", "run_id": "r1", "project_id": P1, "gc_id": "gc-a",
         "gc_name": "Acme Builders", "status": "sent", "sent_at": old},
        {"id": "g2", "run_id": "r1", "project_id": P1, "gc_id": "gc-b",
         "gc_name": "Brite Construction", "status": "sending"},
    ]
    env.install(_db(runs=runs, run_gcs=rows))
    assert asend.reclaim_stuck() == 1
    (bell,) = env.bells
    assert bell[2] == "auto_send.sent" and bell[4]["mirror_email"] is True
    assert "1 GC" in bell[3]


# Finding 23: Send now never reopens an accepted or human-sent run.


def _partial_run(**kw):
    return _sent_run(status="partial", **kw)


def test_send_now_refuses_an_accepted_run(env):
    env.install(_db(runs=[_partial_run(accepted_at="2026-10-20T01:00:00Z")],
                    run_gcs=SENT_ROWS))
    with pytest.raises(AutoSendError) as exc:
        asend.send_now(P1, "u-admin")
    assert exc.value.code == "already_accepted"
    assert env.run()["status"] == "partial" and env.bells == []


def test_send_now_refuses_after_a_human_send(env):
    env.install(_db(runs=[_partial_run()], run_gcs=SENT_ROWS,
                    sends=[{"id": "ps1", "project_id": P1, "gc_id": "gc-b",
                            "status": "sent"}]))
    with pytest.raises(AutoSendError) as exc:
        asend.send_now(P1, "u-admin")
    assert exc.value.code == "already_sent"


def test_manual_claim_never_reopens_an_accepted_run(env):
    env.install(_db(runs=[_partial_run(accepted_at="2026-10-20T01:00:00Z")],
                    run_gcs=SENT_ROWS))
    with pytest.raises(AutoSendError) as exc:
        asend.run_send(P1, BID_DAY, trigger="manual", actor_id="u-admin")
    assert exc.value.code == "already_accepted"


# Finding 21: an accepted auto send re-sends through the real validators.


def test_accepted_auto_send_document_passes_the_resend_gate():
    project = {"id": P1, "name": "Sunset Plaza", "number": "26.10.7301",
               "address": "1 Main St", "labor_time": "day_work",
               "wage_type": "prevailing_wage"}
    lines = ["Underground conduit per E-1.", "Lighting per plans."]
    amounts = {"material": Decimal("1100.00"), "labor": Decimal("2200.00"),
               "total": Decimal("3300.00")}
    base = proposal_send.build_base_context(
        project, {"lines_json": lines},
        {**amounts, "gear": None, "underground": None, "low_voltage": None},
        includes_generator=False,
    )
    docx = asend.render_for_gc(base, "Acme Builders", ["Acme Builders", "Brite Construction"],
                               amounts, proposal_docx.load_template_bytes())
    row = {"gc_id": "gc-a", "project_id": P1, "gc_name": "Acme Builders",
           "origin": "auto_send", "material_amount": "1100.00", "labor_amount": "2200.00",
           "gear_amount": None, "underground_amount": None, "low_voltage_amount": None}
    kwargs = dict(
        file_row={"gc_id": "gc-a", "project_id": P1, "category": "proposal",
                  "filename": proposal_docx.build_filename("26.10.7301", "Acme Builders")},
        docx_bytes=docx, recipients=["ann@acme.test"],
        live_gc={"id": "gc-a", "contacts": [{"email": "ann@acme.test"}]},
        project=project, scope_lines=tuple(lines), other_gc_names=("Brite Construction",),
    )
    proposal_send.assert_resend_isolation(row=row, **kwargs)
    # The same document on a Send Out row (Underground removed) still fails.
    with pytest.raises(Exception, match="Underground"):
        proposal_send.assert_resend_isolation(row={**row, "origin": "send_out"}, **kwargs)


# Finding 27: Accept writes the canonical pricing rows (SQL text).


def test_acceptance_writes_the_committed_pricing_snapshot():
    from pathlib import Path

    migrations = Path(__file__).resolve().parents[1] / "supabase/migrations"
    (path,) = list(migrations.glob("0159_*.sql"))
    sql = path.read_text(encoding="utf-8")
    fn = sql[sql.index("create or replace function apply_auto_send_acceptance"):]
    fn = fn[: fn.index("$$;")]
    for table in ("labor_reviews", "markups", "verifications"):
        assert f"insert into {table}" in fn
    assert "accept_prior_state = v_prior" in fn
    assert "v_run.estimate->>'materials'" in fn
    assert "add column if not exists accept_prior_state jsonb" in sql
    for table in ("project_files", "estimator_submissions"):
        assert (f"alter table {table}\n  add column if not exists impersonated_by uuid"
                in sql)
