"""Personal help uses opaque UUIDs; polling never assumes a numeric ledger."""
from ...agent.help import HelpDesk, WaitResult, _CLOSED


class UserHelpDesk(HelpDesk):
    def describe(self, messages):
        rows = super().describe(messages)
        for row in rows:
            row["id"] = str(row["id"])
        return rows

    def absorb(self, help_id, messages):
        for message in messages:
            self.messages[message.id] = (help_id, message)

    async def wait(self, thread, accept=None):
        self.ctx.set_waiting_help(True)
        try:
            while True:
                response = await self.ctx.client.help_messages(self.ctx.lease, thread.help_id)
                fresh = [m for m in response.messages if m.id not in self.messages]
                self.absorb(thread.help_id, fresh)
                thread.status = response.status
                owner = [m for m in fresh if m.author_kind != "agent"]
                if owner and (accept is None or accept(owner)):
                    return WaitResult(owner, response.status)
                if response.status in _CLOSED:
                    return WaitResult(owner, response.status)
                remaining = ((thread.hold_until or self._now()) - self._now()).total_seconds()
                if remaining <= 0:
                    await self.ctx.client.park(self.ctx.lease)
                    return WaitResult([], response.status, parked=True)
                await self._wake_or_sleep(min(self.poll_seconds, remaining))
        finally:
            self.ctx.set_waiting_help(False)
