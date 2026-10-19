"""Live view / remote control seam (spec 8 "Live view and remote control",
O5). NOT BUILT in v1.

The future design: BDR asks for a live view through the heartbeat `controls`
field (always [] in v1) and the reserved /jobs/{id}/live route (404 in v1).
The worker then starts a CDP `Page.startScreencast` on the job's model page
and streams frames; any remote input arrives as `helper`-actor tool calls
through `Executor.run_tool`, so it is guarded, fenced and audited exactly
like a model call, and the agent loop pauses while the job's control_mode is
'operator'. Login pages are never streamed.
"""

from collections.abc import Callable
from typing import Any


async def start_screencast(page: Any, on_frame: Callable[[bytes], None]) -> None:
    """Seam only: live view is out of scope for v1."""
    raise NotImplementedError("live view is not part of v1 (see docs/BROWSER_AGENTS.md, Future)")
