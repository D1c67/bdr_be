"""Help requests from the worker side (spec 6, O5) and the four help tools.

`HelpDesk` keeps what this job knows about its help threads: the open
request it holds or joined, every helper message and file it has seen, and
what it already turned into RFI contributions. It is shared by:

  - the model's tools: request_help, reply_help, attach_helper_file,
    add_helper_note;
  - ensure_login (login scope help: MFA codes, Retry after a password fix);
  - the loop (cap_reached / provider_config help after a stop reply);
  - resume: the claim's `unconsumed_help` is absorbed before the first turn.

Waiting: the page is held while the job polls every HELP_POLL_SECONDS (woken
early by the heartbeat's help_updates) until a helper writes, the request
closes, or `hold_until` passes; then the job calls /park and the slot ends
without a terminal call (an answer requeues it on BDR). Active time does not
count while waiting. Secret messages never carry a value here; only the
login routine redeems one, through /secrets/{id}/use.
"""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from ..protocol import (
    HELP_POLL_SECONDS,
    ContributionFileItem,
    ContributionNoteItem,
    ContributionsRequest,
    HelpFile,
    HelpOpenRequest,
    HelpOpenResponse,
    UnconsumedHelp,
    WorkerHelpMessage,
)
from ..slot import Finish, HandedOff
from ..browser.executor import ToolError, ToolReturn

log = logging.getLogger("bdr_agent.agent.help")

DEFAULT_HOLD_SECONDS = 900
_CLOSED = ("resolved", "expired", "canceled")


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class HelpThread:
    help_id: UUID
    reason: str
    holder: bool = False
    joined: bool = False
    hold_until: datetime | None = None
    status: str = "open"
    last_id: int = 0


@dataclass
class WaitResult:
    messages: list[WorkerHelpMessage] = field(default_factory=list)
    status: str = "open"
    parked: bool = False


class HelpDesk:
    def __init__(self, ctx: Any, *, now: Callable[[], datetime] = _now,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 poll_seconds: float = HELP_POLL_SECONDS, default_hold_seconds: int = DEFAULT_HOLD_SECONDS,
                 contributions_sent: list[str] | None = None,
                 on_contribution: Callable[[str], Awaitable[None]] | None = None):
        self.ctx = ctx
        self._now = now
        self._sleep = sleep
        self.poll_seconds = poll_seconds
        self.default_hold_seconds = default_hold_seconds
        self.threads: dict[UUID, HelpThread] = {}
        self.current: HelpThread | None = None
        self.messages: dict[int, tuple[UUID, WorkerHelpMessage]] = {}
        self.files: dict[UUID, tuple[UUID, HelpFile]] = {}
        self.sent: set[str] = set(contributions_sent or [])
        self._on_contribution = on_contribution

    # Knowledge ---------------------------------------------------------------------

    def absorb(self, help_id: UUID, messages: list[WorkerHelpMessage]) -> None:
        thread = self.threads.get(help_id)
        for m in messages:
            self.messages[m.id] = (help_id, m)
            for f in m.files:
                self.files[f.artifact_id] = (help_id, f)
            if thread is not None and m.id > thread.last_id:
                thread.last_id = m.id

    def absorb_claim(self, unconsumed: list[UnconsumedHelp]) -> list[int]:
        """Resume: what helpers wrote while the job was parked. Returns the
        helper message ids to report as seen on the first turn."""
        seen: list[int] = []
        for item in unconsumed:
            thread = self.threads.setdefault(item.help_id, HelpThread(item.help_id, reason="resumed",
                                                                      status=item.status))
            thread.status = item.status
            self.absorb(item.help_id, item.messages)
            self.current = thread
            seen += [m.id for m in item.messages if m.author_kind != "agent"]
        return seen

    def describe(self, messages: list[WorkerHelpMessage]) -> list[dict[str, Any]]:
        """Helper messages as the model sees them (BDR wraps them as helper
        text). A secret message shows only that one was sent."""
        out = []
        for m in messages:
            item: dict[str, Any] = {"id": m.id, "from": m.author_label or m.author_kind, "action": m.action}
            if m.secret_ref is not None:
                item["secret"] = True
            elif m.body:
                item["text"] = m.body
            if m.files:
                item["files"] = [{"artifact_id": str(f.artifact_id), "filename": f.filename,
                                  "mime_type": f.mime_type, "size_bytes": f.size_bytes,
                                  "attachable": f.attachable} for f in m.files]
            out.append(item)
        return out

    # BDR calls -------------------------------------------------------------------------

    async def open(self, reason_code: str, message: str | None, screenshot_artifact_id: UUID | None,
                   *, skip_history: bool = False) -> tuple[HelpOpenResponse, HelpThread | None]:
        """Open or join a help request. `skip_history`: when this job joins a
        request it has not seen before, treat everything already on it as
        history, so wait() returns only messages written after the join (a
        login wait must not mistake an old helper note for an answer)."""
        resp = await self.ctx.client.help_open(self.ctx.lease, HelpOpenRequest(
            reason_code=reason_code, message=(message or None) and message[:1000],
            screenshot_artifact_id=screenshot_artifact_id))
        if resp.suppressed or resp.help_id is None:
            self.ctx.steps.log("note", f"Help for {reason_code} is suppressed for this scope")
            return resp, None
        known = resp.help_id in self.threads
        thread = self.threads.get(resp.help_id) or HelpThread(resp.help_id, reason=reason_code)
        thread.holder, thread.joined = resp.holder, resp.joined_existing
        thread.hold_until = resp.hold_until or (self._now() + timedelta(seconds=self.default_hold_seconds))
        thread.status = "open"
        self.threads[resp.help_id] = thread
        self.current = thread
        if skip_history and resp.joined_existing and not known:
            history = await self.ctx.client.help_messages(self.ctx.lease, resp.help_id)
            self.absorb(resp.help_id, history.messages)   # known to the job, but not an answer
        self.ctx.steps.log("note", f"{'Joined' if resp.joined_existing else 'Opened'} help ({reason_code}); "
                                   f"{'holding the page' if resp.holder else 'waiting'}")
        return resp, thread

    async def wait(self, thread: HelpThread,
                   accept: Callable[[list[WorkerHelpMessage]], bool] | None = None) -> WaitResult:
        """Hold the page until a helper writes (and `accept` agrees), the
        request closes, or hold_until passes (then /park)."""
        client, lease = self.ctx.client, self.ctx.lease
        self.ctx.set_waiting_help(True)
        try:
            while True:
                resp = await client.help_messages(lease, thread.help_id, after=thread.last_id or None)
                fresh = [m for m in resp.messages if m.id > thread.last_id]
                self.absorb(thread.help_id, fresh)
                thread.status = resp.status
                from_helper = [m for m in fresh if m.author_kind != "agent"]
                if from_helper and (accept is None or accept(from_helper)):
                    return WaitResult(from_helper, resp.status)
                if resp.status in _CLOSED:
                    return WaitResult(from_helper, resp.status)
                hold_until = thread.hold_until or self._now()
                remaining = (hold_until - self._now()).total_seconds()
                if remaining <= 0:
                    await client.park(lease)
                    self.ctx.steps.log("note", "Help hold ran out; parked until a helper answers")
                    return WaitResult([], resp.status, parked=True)
                await self._wake_or_sleep(min(self.poll_seconds, remaining))
        finally:
            self.ctx.set_waiting_help(False)

    async def _wake_or_sleep(self, seconds: float) -> None:
        queue: asyncio.Queue = self.ctx.help_updates
        if not queue.empty():
            with contextlib.suppress(asyncio.QueueEmpty):
                while True:
                    queue.get_nowait()
            return
        getter = asyncio.ensure_future(queue.get())
        sleeper = asyncio.ensure_future(self._sleep(seconds))
        try:
            await asyncio.wait({getter, sleeper}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in (getter, sleeper):
                if not t.done():
                    t.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await t

    async def consume(self, help_id: UUID, message_ids: list[int]) -> None:
        ids = sorted(set(message_ids))
        if ids:
            await self.ctx.client.help_consume(self.ctx.lease, help_id, ids)

    async def consume_seen(self, message_ids: list[int]) -> None:
        by_help: dict[UUID, list[int]] = {}
        for mid in message_ids:
            entry = self.messages.get(mid)
            if entry is not None and entry[1].secret_ref is None:
                by_help.setdefault(entry[0], []).append(mid)
        for help_id, ids in by_help.items():
            await self.consume(help_id, ids)

    async def record(self, key: str) -> None:
        self.sent.add(key)
        if self._on_contribution is not None:
            await self._on_contribution(key)


# The four help tools -----------------------------------------------------------------


class HelpTools:
    """Handlers for request_help, reply_help, attach_helper_file and
    add_helper_note. `rfi_id` tells which RFI a contribution belongs to (None
    until the job knows it)."""

    def __init__(self, desk: HelpDesk, executor: Any, *, rfi_id: Callable[[], str | None]):
        self.desk = desk
        self.ex = executor
        self._rfi_id = rfi_id

    def handlers(self) -> dict[str, Callable[[Any], Awaitable[ToolReturn]]]:
        return {"request_help": self.request_help, "reply_help": self.reply_help,
                "attach_helper_file": self.attach_helper_file, "add_helper_note": self.add_helper_note}

    async def request_help(self, args: Any) -> ToolReturn:
        shot = await self.ex.screenshot(label="help")
        resp, thread = await self.desk.open(args.reason_code, args.message, shot)
        if thread is None:
            return ToolReturn({"suppressed": True}, ok=False, terminal=Finish(
                "failed", "Help for this project was declined recently", error_kind="access_denied_suppressed",
                error="help suppressed for this scope"))
        result = await self.desk.wait(thread)
        if result.parked:
            return ToolReturn({"parked": True}, terminal=HandedOff("Parked waiting for help"))
        ids = [m.id for m in result.messages]
        await self.desk.consume_seen(ids)
        return ToolReturn({"help_id": str(thread.help_id), "status": result.status,
                           "messages": self.desk.describe(result.messages)},
                          ledger={"help_id": str(thread.help_id), "status": result.status,
                                  "message_ids": ids},
                          helper_message_ids=ids)

    async def reply_help(self, args: Any) -> ToolReturn:
        thread = self.desk.current
        if thread is None:
            raise ToolError("no_help_open", "request_help first")
        resp = await self.ex.ctx.client.help_reply(self.ex.ctx.lease, thread.help_id, args.text)
        return ToolReturn({"sent": True, "message_id": resp.id}, screenshot=False)

    def _need_rfi(self) -> str:
        rfi = self._rfi_id()
        if not rfi:
            raise ToolError("rfi_unknown", "find the RFI first (capture_rfi_detail or procore_list_rfis)")
        return rfi

    async def attach_helper_file(self, args: Any) -> ToolReturn:
        entry = self.desk.files.get(args.artifact_id)
        if entry is None:
            raise ToolError("unknown_file", "that file was not shared in this job's help")
        help_id, hf = entry
        if not hf.attachable:
            raise ToolError("not_attachable", "this file type can stay in the help thread only")
        rfi = self._need_rfi()
        key = f"file:{hf.artifact_id}:{rfi}"
        if key in self.desk.sent:
            return ToolReturn({"added": True, "already": True}, screenshot=False)
        req = ContributionsRequest(help_id=help_id, external_rfi_id=rfi, items=[
            ContributionFileItem(kind=f"file_{args.kind}", artifact_id=hf.artifact_id)])  # type: ignore[arg-type]
        return await self._send(req, key)

    async def add_helper_note(self, args: Any) -> ToolReturn:
        helps = set()
        for mid in args.message_ids:
            entry = self.desk.messages.get(mid)
            if entry is None or entry[1].author_kind != "helper" or not entry[1].body:
                raise ToolError("not_helper_message", f"message {mid} is not a helper's text message")
            helps.add(entry[0])
        if len(helps) != 1:
            raise ToolError("mixed_help", "all messages must come from one help request")
        rfi = self._need_rfi()
        ids = sorted(set(args.message_ids))
        key = f"note:{','.join(str(i) for i in ids)}:{rfi}"
        if key in self.desk.sent:
            return ToolReturn({"added": True, "already": True}, screenshot=False)
        req = ContributionsRequest(help_id=helps.pop(), external_rfi_id=rfi,
                                   items=[ContributionNoteItem(kind="note", message_ids=ids)])
        return await self._send(req, key)

    async def _send(self, req: ContributionsRequest, key: str) -> ToolReturn:
        resp = await self.ex.ctx.client.contributions(self.ex.ctx.lease, req)
        if resp.rejected:
            reason = resp.rejected[0].reason
            if reason == "duplicate":
                await self.desk.record(key)
                return ToolReturn({"added": True, "already": True}, screenshot=False)
            raise ToolError("rejected", reason)
        await self.desk.record(key)
        return ToolReturn({"added": True, "contribution_ids": [str(a.contribution_id) for a in resp.accepted]},
                          screenshot=False)
