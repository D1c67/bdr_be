"""Skills on disk (docs/ASSISTANT.md, Skills and tools): one
app/assistant/skills/<name>/SKILL.md per skill with Agent Skills
frontmatter. Only name and description go in the prompt; the body loads
through the load_skill tool, which is a route (GET /assistant/skills/{name}).

Frontmatter is the fixed shape below, parsed here without a YAML
dependency:

    ---
    name: bdr-glossary
    description: One line.
    metadata:
      requires_flags: [bidding]
      roles: [executive, it_admin]
      tools: [plans_list_runs, plans_get_brief]
      version: "1"
    ---

Phase 4 (docs/ASSISTANT_PHASE4_PLAN.md 3.16.5): `metadata.tools` names the
deferred tools the skill uses. Loading the skill surfaces them for the rest
of the turn (and the next turn's first round). Every name must be a tool
name; that each one is registered and deferred is pinned by a test, so this
module stays free of the registry.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.roles import INTERNAL_ROLES, Role

SKILLS_DIR = Path(__file__).resolve().parent / "skills"
SKILL_FILE = "SKILL.md"
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,59}$")
_LIST_RE = re.compile(r"^\[(.*)\]$")
_TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,60}$")


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str
    requires_flags: tuple[str, ...] = ()
    roles: frozenset[Role] = field(default_factory=lambda: INTERNAL_ROLES)
    version: str = "1"
    path: str = ""
    # Phase 4 (3.16.5): the deferred tools a loaded skill surfaces.
    tools: tuple[str, ...] = ()

    def available_to(self, role: Role | None, enabled_map: Mapping[str, bool]) -> bool:
        if role is None or role not in self.roles:
            return False
        return all(enabled_map.get(flag, False) for flag in self.requires_flags)


class SkillError(ValueError):
    pass


def _parse_list(value: str) -> tuple[str, ...]:
    value = value.strip()
    m = _LIST_RE.match(value)
    inner = m.group(1) if m else value
    return tuple(v.strip().strip("'\"") for v in inner.split(",") if v.strip())


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---"):
        raise SkillError("SKILL.md must start with frontmatter")
    parts = text.split("\n---", 2)
    if len(parts) < 2:
        raise SkillError("SKILL.md frontmatter is not closed")
    head = parts[0][3:]
    body = parts[1].lstrip("\n") if len(parts) == 2 else (parts[1] + "\n---" + parts[2]).lstrip("\n")
    meta: dict[str, Any] = {}
    section: dict[str, Any] | None = None
    for raw in head.splitlines():
        if not raw.strip() or raw.strip().startswith("#"):
            continue
        indented = raw.startswith((" ", "\t"))
        key, sep, value = raw.strip().partition(":")
        if not sep:
            raise SkillError(f"bad frontmatter line: {raw!r}")
        key, value = key.strip(), value.strip()
        if indented and section is not None:
            section[key] = value
            continue
        if value == "":
            section = {}
            meta[key] = section
        else:
            section = None
            meta[key] = value.strip("'\"")
    return meta, body


def load_skill_file(path: Path) -> Skill:
    text = path.read_text(encoding="utf-8")
    if "\u2014" in text:
        raise SkillError(f"{path.name}: no em dashes in skill files")
    meta, body = parse_frontmatter(text)
    name = str(meta.get("name") or "")
    if not _NAME_RE.match(name) or name != path.parent.name:
        raise SkillError(f"{path}: name must match the folder ({path.parent.name})")
    description = str(meta.get("description") or "").strip()
    if not description:
        raise SkillError(f"{path}: description is required")
    md = meta.get("metadata") if isinstance(meta.get("metadata"), dict) else {}
    flags = _parse_list(str(md.get("requires_flags") or "[]"))
    role_names = _parse_list(str(md.get("roles") or "[]"))
    roles = frozenset(Role(r) for r in role_names) if role_names else INTERNAL_ROLES
    if not roles <= INTERNAL_ROLES:
        raise SkillError(f"{path}: roles must be internal roles")
    tools = _parse_list(str(md.get("tools") or "[]"))
    for tool in tools:
        if not _TOOL_NAME_RE.match(tool):
            raise SkillError(f"{path}: metadata.tools entry {tool!r} is not a tool name")
    return Skill(
        name=name, description=description, body=body.strip(), requires_flags=flags,
        roles=roles, version=str(md.get("version") or "1").strip("'\""), path=str(path),
        tools=tuple(dict.fromkeys(tools)),
    )


def load_all(root: Path | None = None) -> dict[str, Skill]:
    base = root or SKILLS_DIR
    out: dict[str, Skill] = {}
    if not base.is_dir():
        return out
    for folder in sorted(p for p in base.iterdir() if p.is_dir()):
        file = folder / SKILL_FILE
        if file.is_file():
            skill = load_skill_file(file)
            out[skill.name] = skill
    return out


def available(role: Role | None, enabled_map: Mapping[str, bool], root: Path | None = None) -> list[Skill]:
    return [s for s in load_all(root).values() if s.available_to(role, enabled_map)]


def get(name: str, role: Role | None, enabled_map: Mapping[str, bool], root: Path | None = None) -> Skill | None:
    skill = load_all(root).get(str(name or "").strip().lower())
    if skill is None or not skill.available_to(role, enabled_map):
        return None
    return skill
