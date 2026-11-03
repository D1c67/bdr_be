"""Tool modules, one per namespace (docs/ASSISTANT.md, Skills and tools).

Every tool is a ToolSpec mapping to real routes of the served app and is
executed through the shim; nothing in this package imports the Supabase
client or reads a table (tests/test_assistant_repo_isolation pins it).
`load_all()` imports every namespace so their `register(...)` calls run.
"""

from __future__ import annotations

import importlib

NAMESPACE_MODULES = (
    "app.assistant.tools.core",
    "app.assistant.tools.bidding",
    # Phase 1 (docs/ASSISTANT_PHASE1_PLAN.md 3.7): the confirm write tools.
    "app.assistant.tools.writes",
    "app.assistant.tools.todos",
    # Phase 2 (docs/ASSISTANT_PHASE2_PLAN.md 3.10): the splitter handoff.
    "app.assistant.tools.splits",
    # Phase 3 (docs/ASSISTANT_PHASE3_PLAN.md 3.6 and 3.8): routines and notes.
    "app.assistant.tools.schedules",
    "app.assistant.tools.memory",
    # Phase 4 (docs/ASSISTANT_PHASE4_PLAN.md 3.17): plans, lanes, PM and
    # payroll (WI-13a), reports and evidence (WI-13b).
    "app.assistant.tools.plans",
    "app.assistant.tools.workflow",
    "app.assistant.tools.pm",
    "app.assistant.tools.payroll",
    "app.assistant.tools.reports",
    "app.assistant.tools.evidence",
    "app.assistant.tools.web",
    "app.assistant.tools.mail",
    "app.assistant.tools.browser",
    # Phase 8 (docs/ASSISTANT_PHASE8_PLAN.md 6.1): inbox_list.
    "app.assistant.tools.inbox",
)


def load_all() -> None:
    for name in NAMESPACE_MODULES:
        importlib.import_module(name)
