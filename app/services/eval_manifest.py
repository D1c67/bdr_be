"""Golden manifest, tiers, populations, blind guard and exclusions for the
AI Estimating eval (WS0; spec 8.1, 8.7, 5.3 `excluded_projects`).

The tiers of 8.1 (3 smoke, 5 core, 4 holdout) and the 66 golden project ids
are data in this module: nothing is swapped in from the pool, and the
holdout never feeds mining, libraries, thresholds, prompts or adjudication
tuning. `excluded_projects(purpose, case_id)` is the one API every miner
and tuner calls; it always returns the holdout projects plus the case's own
project (leave-one-project-out), so a sweep on a case never sees that
case's truth.

`blind_guard(case, uploads, stage)` refuses answer files before an eval
upload: a file whose sha256 equals any golden answer file, a file in an
answer category, a filename that names an answer (BOQ, ESTIMATE, MARKED,
TAKEOFF, MARKUP, RFQ, PROPOSAL, BID RECAP) without a written override, and
any vendor quote in a quantity stage (quotes repeat estimator quantities).

`build_manifest(backup_dir)` reads the backup once (read-only), hashes every
input and answer file of the 12 cases, counts big sheets (pages of at least
24 x 18 in in drawing, electrical_drawing and addendum PDFs) and spec pages,
and writes `EVAL_ROOT/manifest/manifest.json`. Populations of 8.7 are
filters over the backup (`population(name)`), holdout excluded unless the
population says otherwise.

This module never touches a database or a model.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.services.eval_reference import (
    DEFAULT_BACKUP_DIR,
    BackupData,
    g3_truth,
    now_pacific_iso,
    sha256_file,
    to_pacific_iso,
)

MANIFEST_SCHEMA = "bdr.eval.manifest/1"
TIERS: tuple[str, ...] = ("smoke", "core", "holdout", "pool")
STAGES: tuple[str, ...] = ("quantity", "pricing")
PURPOSES: tuple[str, ...] = (
    "mining",
    "pricing_library",
    "prompt_examples",
    "thresholds",
    "scope_policy",
    "bands",
    "missed_list",
    "aliases",
    "calibration",
)
ANSWER_CATEGORIES: frozenset[str] = frozenset(
    {"boq", "estimate", "marked_plans", "markup", "rfq_split", "proposal", "revision", "estimator_additional"}
)
QUANTITY_INPUT_CATEGORIES: tuple[str, ...] = ("drawing", "electrical_drawing", "specification", "addendum", "other")
PRICING_INPUT_CATEGORIES: tuple[str, ...] = ("quote",)
BIG_SHEET_MIN_IN: tuple[float, float] = (24.0, 18.0)
ANSWER_NAME_RE = re.compile(
    r"\b(BOQ|COUNTS?|ESTIMATE|MARKED|TAKE ?OFF|MARK ?UP|RFQ|PROPOSAL|BID RECAP)\b", re.I
)

# Section 8.1, fixed. Project id prefixes are resolved against the backup.
TIER_CASES: dict[str, tuple[tuple[str, str, str], ...]] = {
    "smoke": (
        ("7154", "7dfc64e6", "UMC Outpatient Pharmacy"),
        ("7177", "ba167b17", "YMCA Torrey Pines"),
        ("7172", "dc0cb2f0", "Five Star Resolution"),
    ),
    "core": (
        ("7181", "2bf3b21e", "Windmill Library Conf Room"),
        ("7158", "061335de", "Specialty Veterinary Hospital"),
        ("7196", "2b3586cd", "Sloan Road"),
        ("7179", "c45e3f91", "Replace Interior Lighting"),
        ("7167", "b9bd3846", "Gaudin Ford CVC Facelift"),
    ),
    "holdout": (
        ("7188", "078c470d", "Cimarron-Memorial HS"),
        ("7173", "ba9f0bc2", "Bob Price Rec Center"),
        ("7178", "220937f8", "UMC MLK Warehouse Remodel"),
        ("7148", "346a995e", "Palazzo 1730C Boutique"),
    ),
}
CASE_TIER: dict[str, str] = {case: tier for tier, rows in TIER_CASES.items() for case, _p, _n in rows}
CASE_PREFIX: dict[str, str] = {case: prefix for rows in TIER_CASES.values() for case, prefix, _n in rows}
PREFIX_CASE: dict[str, str] = {prefix: case for case, prefix in CASE_PREFIX.items()}
HOLDOUT_PREFIXES: frozenset[str] = frozenset(p for _c, p, _n in TIER_CASES["holdout"])
# Pre-named core drop order when a sweep's dry run exceeds the budget (8.4).
CORE_DROP_ORDER: tuple[str, ...] = ("7179", "7158")
# Old-pipeline bundles of 8.7 (the 7172 and 7177 ids are dev runs recorded
# by the integrator in WS0 step 7).
OLD_PIPELINE_BUNDLES: dict[str, str | None] = {"7154": "ff8fad25", "7172": None, "7177": None}

# Per-case notes written by WS0 step 3 (spec 8.1).
CASE_NOTES: dict[str, dict[str, Any]] = {
    "7177": {
        "misfiled_inputs": {
            "2026-08-28_ITB_100028_Addendum_No._02_LVCC_Central_Hall_Airwalls.pdf": (
                "Addendum No. 02 of ITB 100028, Central Hall Airwalls Project at the Las Vegas "
                "Convention Center (PWP CL-2026-433, owner LVCVA, 3150 Paradise Rd). It belongs to "
                "project 26.8.7171 LVCC Central Hall Airwalls, not to 7177 YMCA Torrey Pines "
                "(2900 N. Torrey Pines). Its 2 large pages are architectural (A0.00, A2.00). "
                "Excluded from the 7177 inputs; the case has 7 big sheets, not 9."
            )
        },
        "note": "The quote category holds email attachments that are drawings (E0.30, E5.01, Trench Markup); the blind guard keeps every quote out of the quantity stage anyway.",
    },
    "7154": {"note": "Training case (1.3 item 5): the stored prod run ff8fad25 is the old-pipeline bundle; ELEC and MECH sets sit in the drawing folder with category electrical_drawing (3.2)."},
    "7188": {"note": "Two sends with different totals: truth is the latest sent_at, the spread is reported (8.1)."},
}

# The 66 golden ids (8.1): drawings + BOQ + estimate in the backup.
GOLDEN_IDS: tuple[str, ...] = (
    "026b2dc5-676f-4ebd-b9a2-b744238b4049", "061335de-ac0c-497f-b1da-b214a5bb112d", "078c470d-efa1-479c-b579-1690f5da47a0",
    "083dd5f3-e604-44c6-b299-fd17f4a531f5", "0b68e4f9-7950-42fb-abc4-04a451e41236", "0d03bc95-407f-47c0-8369-1fbed9c7a4cb",
    "0d81db15-45ab-40cd-a7cf-f029b1783712", "114e2536-c3f7-43cd-9eb8-1e88a98a988a", "1b21c1c6-d12f-4b31-874e-976450af24e7",
    "1c1eb1fa-b967-41f8-8b89-647ab8a5e32b", "220937f8-c69d-43b2-b5f8-b2e50f350310", "26ce6d73-e313-432c-a9b4-b5452a0e8b7b",
    "27789224-5aee-43e1-ac6e-9559bc8f9ff1", "2b3586cd-0364-4ace-a933-bd3efa353933", "2bf3b21e-5ce4-406c-aafe-592dbc7d00e7",
    "301b4da2-0a80-4536-b168-638ffce5d7f8", "346a995e-3758-40cc-994c-008e00f299c6", "39b6ed51-4d3d-4d82-95a0-ed857b1c0fff",
    "3ae4c335-a2f1-4b27-8460-e3e62f2ca984", "45b39974-ba06-48be-b5ac-42cbcfbf7c90", "4a382f4b-237d-4a33-be1d-6daa902a2521",
    "4ef93bcb-c114-41da-9892-ac5758cf07be", "5982a438-8e91-4440-a359-09bc0a7ee9cb", "5e224aab-8637-452b-9d84-c4ed3c353a3c",
    "63f3e354-1434-4b31-ab36-d124bfbcb8aa", "6961f40a-4a89-4d98-9382-8e27d4d7466d", "6b441d52-c423-476d-ae2f-34e2ec9607b0",
    "7357cc7c-1878-43ce-9d4e-760b31a00a08", "7589a596-6785-40a3-bb05-92760c941a52", "7dfc64e6-36ef-4c67-9d02-0d7942015011",
    "82b14adb-e8e6-41cd-93af-5f929fa3ad05", "84f0771e-597b-41e2-a6ac-91399dbffdbd", "8804e10a-a87b-40ab-a4a2-c86390b66002",
    "88319400-db76-4cfc-8c21-f8d254097935", "895cd2d5-bf9a-4b50-ac04-aee64a535943", "8c08ff60-f9cc-43de-932e-03362f641033",
    "8e4d09c4-8faf-4b5a-9407-3c038612ea84", "91c65c7c-d658-4fb8-9c4c-e17efe116fd8", "9b772ebf-4ad0-4830-a945-dd91fb81bb1c",
    "9d1de4ea-8293-48d7-9e0d-c7c370cda370", "9ee491b9-1821-4d5d-bee0-b1795bcabac5", "aec676fa-55b0-4253-83cd-151482ed34fe",
    "b17205ef-0fe8-4994-9c47-092d7a14d6b4", "b185155d-c74d-442e-9ea5-04b8cb3fc023", "b2211f30-3ffb-4bea-ac8a-24925113134a",
    "b9bd3846-9759-4847-ac1d-19a349c83ab3", "ba167b17-0ff5-4012-a638-403bcced09af", "ba9f0bc2-ef71-4bfe-8b4f-70f92cf0c4b9",
    "c400b8fd-00bd-465d-ba4c-ae602acdf4ed", "c4428db0-41c1-4345-8471-4c23050cd0c6", "c45e3f91-133e-4b1b-bc1b-49e2c8b36f02",
    "cab0a700-bf0e-497c-abc2-75a46c9449bb", "cd4ee594-220b-4df7-96c1-28ab050070e0", "cfeb91fc-691f-4efd-bc8c-312bb7c51a66",
    "d3efbef9-cda3-43f7-8143-fd29822881b1", "db9ee88d-bfa5-4ecb-ae16-98bce44865cf", "dc0cb2f0-bc93-4bb5-9a17-79adf151a5fb",
    "e70b9bdc-ed55-4a87-bc6b-db408489d077", "e858f05a-e763-48f2-9f75-5b4bc8e3ffc2", "e9ad59d9-d9f3-4d8c-904e-b35bb7f1aed0",
    "f33871cc-b673-42fd-ba1e-36e2c3bf17c9", "f9182cfc-978c-4d30-8717-ed5aab09b6c8", "f9be8b69-3b1b-44f0-93d8-0f72e07ad937",
    "fb767cc1-80a7-4064-8745-e046445bc8b0", "fdd8eb5e-ceff-4369-864a-a2bd6692a140", "ff059866-b011-4878-b90f-634b42f4e88e",
)
assert len(GOLDEN_IDS) == 66 and len(set(GOLDEN_IDS)) == 66
HOLDOUT_IDS: frozenset[str] = frozenset(g for g in GOLDEN_IDS if g[:8] in HOLDOUT_PREFIXES)
assert len(HOLDOUT_IDS) == 4
TIER_IDS: dict[str, frozenset[str]] = {
    tier: frozenset(g for g in GOLDEN_IDS if g[:8] in {p for _c, p, _n in rows})
    for tier, rows in TIER_CASES.items()
}
TIER_IDS["pool"] = frozenset(GOLDEN_IDS) - TIER_IDS["smoke"] - TIER_IDS["core"] - TIER_IDS["holdout"]
assert len(TIER_IDS["pool"]) == 54
POPULATIONS: tuple[str, ...] = (
    "golden",
    "lopo",
    "estimate",
    "wage",
    "markup",
    "bands",
    "r3",
    "tagged_training",
    "old_pipeline_bundles",
)
R3_MATCH_RE = re.compile(r"LINEAR|STRIP|TAPE|TRACK|UNDERCAB|COVE")
R3_EXCLUDE_RE = re.compile(r"HEAD|END CAP|\d+\s*(FT|')\b")


def eval_root() -> Path:
    return Path(get_settings().eval_root)


def manifest_path(root: Path | str | None = None) -> Path:
    return Path(root) if root is not None else eval_root()


# Shapes ---------------------------------------------------------------------------


class BlindViolation(RuntimeError):
    """An eval upload would leak an answer into a run."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class CaseInput:
    project_file_id: str
    filename: str
    category: str
    sha256: str
    label: str | None
    stage_scope: str  # quantity | pricing
    backup_path: str
    pages: int | None = None
    big_pages: int | None = None
    misfiled: bool = False
    note: str | None = None


@dataclass(frozen=True)
class CaseAnswer:
    project_file_id: str
    filename: str
    kind: str
    sha256: str
    backup_path: str
    # True when the same bytes are also a quantity input of the project (an
    # addendum re-uploaded under estimate, for example): such a file is not
    # an answer and never enters the answer-hash table.
    input_copy: bool = False


@dataclass(frozen=True)
class Case:
    case_id: str
    project_id: str
    number: str
    name: str
    tier: str
    project_type: str | None
    wage_type: str | None
    inputs: tuple[CaseInput, ...]
    answers: tuple[CaseAnswer, ...]
    reference_path: str
    reviewed: bool
    blind_overrides: tuple[str, ...]
    bundles: tuple[str, ...]
    notes: str
    big_sheets: int
    spec_pages: int
    sent_truth: bool = False
    committed_verify: bool = False
    sends: int = 0
    distinct_sent_totals: int = 0
    misfiled_inputs: tuple[str, ...] = ()

    @property
    def quantity_inputs(self) -> tuple[CaseInput, ...]:
        return tuple(i for i in self.inputs if i.stage_scope == "quantity" and not i.misfiled)

    @property
    def pricing_inputs(self) -> tuple[CaseInput, ...]:
        return tuple(i for i in self.inputs if i.stage_scope == "pricing" and not i.misfiled)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["inputs"] = [asdict(i) for i in self.inputs]
        d["answers"] = [asdict(a) for a in self.answers]
        return d

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Case:
        return cls(
            case_id=str(d["case_id"]),
            project_id=str(d["project_id"]),
            number=str(d.get("number") or ""),
            name=str(d.get("name") or ""),
            tier=str(d["tier"]),
            project_type=d.get("project_type"),
            wage_type=d.get("wage_type"),
            inputs=tuple(CaseInput(**i) for i in d.get("inputs") or ()),
            answers=tuple(CaseAnswer(**a) for a in d.get("answers") or ()),
            reference_path=str(d.get("reference_path") or ""),
            reviewed=bool(d.get("reviewed")),
            blind_overrides=tuple(d.get("blind_overrides") or ()),
            bundles=tuple(d.get("bundles") or ()),
            notes=str(d.get("notes") or ""),
            big_sheets=int(d.get("big_sheets") or 0),
            spec_pages=int(d.get("spec_pages") or 0),
            sent_truth=bool(d.get("sent_truth")),
            committed_verify=bool(d.get("committed_verify")),
            sends=int(d.get("sends") or 0),
            distinct_sent_totals=int(d.get("distinct_sent_totals") or 0),
            misfiled_inputs=tuple(d.get("misfiled_inputs") or ()),
        )


@dataclass
class Manifest:
    schema: str
    built_at: str
    backup_dir: str
    cases: list[Case]
    golden_ids: list[str]
    holdout_ids: list[str]
    answer_sha256: dict[str, str] = field(default_factory=dict)  # sha256 -> "case_id:filename"
    populations: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def case(self, case_id: str) -> Case:
        for c in self.cases:
            if c.case_id == case_id or c.project_id == case_id or c.project_id.startswith(case_id):
                return c
        raise KeyError(f"no case {case_id!r} in the manifest")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "built_at": self.built_at,
            "backup_dir": self.backup_dir,
            "tiers": {tier: [c.case_id for c in self.cases if c.tier == tier] for tier in ("smoke", "core", "holdout")},
            "core_drop_order": list(CORE_DROP_ORDER),
            "cases": [c.to_dict() for c in self.cases],
            "golden_ids": self.golden_ids,
            "holdout_ids": self.holdout_ids,
            "answer_sha256": self.answer_sha256,
            "populations": self.populations,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Manifest:
        return cls(
            schema=str(d.get("schema") or MANIFEST_SCHEMA),
            built_at=str(d.get("built_at") or ""),
            backup_dir=str(d.get("backup_dir") or ""),
            cases=[Case.from_dict(c) for c in d.get("cases") or ()],
            golden_ids=list(d.get("golden_ids") or GOLDEN_IDS),
            holdout_ids=list(d.get("holdout_ids") or sorted(HOLDOUT_IDS)),
            answer_sha256=dict(d.get("answer_sha256") or {}),
            populations=dict(d.get("populations") or {}),
            notes=list(d.get("notes") or []),
        )


# Tiers and exclusions -------------------------------------------------------------------


def tier_of(project_or_case: str) -> str:
    """smoke | core | holdout | pool | none for a case id, a project id or a prefix."""
    if project_or_case in CASE_TIER:
        return CASE_TIER[project_or_case]
    for tier in ("smoke", "core", "holdout", "pool"):
        if any(g.startswith(project_or_case) for g in TIER_IDS[tier]):
            return tier
    return "none"


def resolve_case_project(case_id: str | None) -> str | None:
    """Full golden project id for a case id, a project id or a prefix; None
    for None. A string that matches no golden project is returned as is
    (a non-golden project excludes itself only)."""
    if case_id is None:
        return None
    prefix = CASE_PREFIX.get(case_id, case_id)
    matches = [g for g in GOLDEN_IDS if g.startswith(prefix)]
    if len(matches) == 1:
        return matches[0]
    return case_id


def excluded_projects(purpose: str, case_id: str | None = None) -> frozenset[str]:
    """Project ids a miner, library builder, tuner or band builder must drop
    for `purpose`: the holdout projects always, plus the case's own project
    (leave-one-project-out) when `case_id` is given. `case_id` may be a case
    id (7154), a project id or its prefix."""
    if purpose not in PURPOSES:
        raise ValueError(f"unknown exclusion purpose {purpose!r}; one of {', '.join(PURPOSES)}")
    out = set(HOLDOUT_IDS)
    own = resolve_case_project(case_id)
    if own:
        out.add(own)
    return frozenset(out)


def excluded_project_ids_sha256(project_ids: Iterable[str]) -> str:
    """sha256 of the sorted excluded ids, one per line (GC4 / contracts)."""
    joined = "\n".join(sorted({str(p) for p in project_ids}))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


# Blind guard -----------------------------------------------------------------------------


def blind_guard(
    case: Case | None,
    uploads: Sequence[CaseInput | Mapping[str, Any]],
    *,
    stage: str,
    answer_hashes: Mapping[str, str] | None = None,
) -> None:
    """Raise BlindViolation when any upload would leak an answer.

    1. `answer_hash_leak`: sha256 equals an answer file of ANY golden case
       (`answer_hashes` from the manifest, plus the case's own answers);
    2. `answer_category`: category in ANSWER_CATEGORIES;
    3. `answer_name`: filename matches ANSWER_NAME_RE and is not listed in
       the case's `blind_overrides` (each override needs a written reason
       in the manifest notes);
    4. `quote_in_quantity_stage`: a quote while `stage == "quantity"`.
    """
    if stage not in STAGES:
        raise ValueError(f"stage must be one of {STAGES}; got {stage!r}")
    hashes: dict[str, str] = dict(answer_hashes or {})
    overrides: set[str] = set()
    if case is not None:
        for a in case.answers:
            if a.sha256 and not a.input_copy:
                hashes.setdefault(a.sha256, f"{case.case_id}:{a.filename}")
        overrides = set(case.blind_overrides)
    for up in uploads:
        if isinstance(up, CaseInput):
            filename, category, sha = up.filename, up.category, up.sha256
        else:
            filename = str(up.get("filename") or "")
            category = str(up.get("category") or "")
            sha = str(up.get("sha256") or "")
        if sha and sha in hashes:
            raise BlindViolation("answer_hash_leak", f"{filename} has the sha256 of answer file {hashes[sha]}")
        if category in ANSWER_CATEGORIES:
            raise BlindViolation("answer_category", f"{filename} is in answer category {category}")
        if ANSWER_NAME_RE.search(filename) and filename not in overrides:
            raise BlindViolation("answer_name", f"{filename} names an answer file (no blind override)")
        if category in PRICING_INPUT_CATEGORIES and stage == "quantity":
            raise BlindViolation("quote_in_quantity_stage", f"{filename} is a vendor quote; quotes never reach a quantity-reading group")


# Build ------------------------------------------------------------------------------------


def _pdf_page_sizes(path: Path) -> list[tuple[float, float]]:
    """(width_in, height_in) per page; empty when the file is not a readable PDF."""
    if path.suffix.lower() != ".pdf" or not path.exists():
        return []
    try:
        import pypdfium2 as pdfium  # local import: optional for non-PDF callers
    except Exception:  # noqa: BLE001
        return []
    try:
        doc = pdfium.PdfDocument(str(path))
    except Exception:  # noqa: BLE001
        return []
    sizes: list[tuple[float, float]] = []
    try:
        for i in range(len(doc)):
            page = doc[i]
            w, h = page.get_size()
            sizes.append((w / 72.0, h / 72.0))
            page.close()
    finally:
        doc.close()
    return sizes


def is_big_sheet(w_in: float, h_in: float) -> bool:
    lo, hi = sorted((w_in, h_in))
    return hi >= BIG_SHEET_MIN_IN[0] and lo >= BIG_SHEET_MIN_IN[1]


def _label_for(category: str) -> str | None:
    return {
        "electrical_drawing": "electrical_drawings",
        "specification": "specifications",
        "addendum": "addenda",
        "other": "other",
    }.get(category)


def build_case(
    data: BackupData,
    case_id: str,
    prefix: str,
    tier: str,
    *,
    eval_root_dir: Path,
    count_pages: bool = True,
) -> Case:
    project_id = data.resolve_project_id(prefix)
    project = data.project(project_id) or {}
    notes = CASE_NOTES.get(case_id, {})
    misfiled = notes.get("misfiled_inputs") or {}
    inputs: list[CaseInput] = []
    answers: list[CaseAnswer] = []
    big_sheets = 0
    spec_pages = 0
    files = data.files(project_id)
    hashes: dict[str, str] = {}
    for row in files:
        path = data.file_path(row)
        hashes[str(row["id"])] = sha256_file(path) if path.exists() else ""
    input_hashes = {
        hashes[str(row["id"])]
        for row in files
        if row.get("category") in QUANTITY_INPUT_CATEGORIES and hashes[str(row["id"])]
    }
    for row in files:
        category = str(row.get("category") or "")
        path = data.file_path(row)
        sha = hashes[str(row["id"])]
        filename = str(row.get("filename") or "")
        if category in ANSWER_CATEGORIES:
            answers.append(
                CaseAnswer(str(row["id"]), filename, category, sha, str(row["storage_path"]), input_copy=sha in input_hashes)
            )
            continue
        if category in QUANTITY_INPUT_CATEGORIES or category in PRICING_INPUT_CATEGORIES:
            scope = "pricing" if category in PRICING_INPUT_CATEGORIES else "quantity"
            pages = big = None
            if count_pages and scope == "quantity":
                sizes = _pdf_page_sizes(path)
                pages = len(sizes) if sizes else None
                big = sum(1 for w, h in sizes if is_big_sheet(w, h)) if sizes else None
            is_misfiled = filename in misfiled
            if scope == "quantity" and not is_misfiled:
                if category in ("drawing", "electrical_drawing", "addendum"):
                    big_sheets += big or 0
                if category == "specification":
                    spec_pages += pages or 0
            inputs.append(
                CaseInput(
                    project_file_id=str(row["id"]),
                    filename=filename,
                    category=category,
                    sha256=sha,
                    label=_label_for(category),
                    stage_scope=scope,
                    backup_path=str(row["storage_path"]),
                    pages=pages,
                    big_pages=big,
                    misfiled=is_misfiled,
                    note=misfiled.get(filename),
                )
            )
    truth = g3_truth(data, project_id)
    reference = eval_root_dir / "references" / case_id / "reference.json"
    return Case(
        case_id=case_id,
        project_id=project_id,
        number=str(project.get("number") or ""),
        name=str(project.get("name") or ""),
        tier=tier,
        project_type=project.get("project_type"),
        wage_type=project.get("wage_type"),
        inputs=tuple(inputs),
        answers=tuple(answers),
        reference_path=str(reference),
        reviewed=False,
        blind_overrides=(),
        bundles=tuple(b for b in [OLD_PIPELINE_BUNDLES.get(case_id)] if b),
        notes=str(notes.get("note") or ""),
        big_sheets=big_sheets,
        spec_pages=spec_pages,
        sent_truth=truth["g3_sent"]["total"] is not None,
        committed_verify=truth["g3_verify"]["sum"] is not None,
        sends=int(truth["g3_sent"]["sends"]),
        distinct_sent_totals=len(truth["g3_sent"]["distinct_totals"]),
        misfiled_inputs=tuple(sorted(misfiled)),
    )


def population(name: str, data: BackupData | None = None, *, backup_dir: Path | str = DEFAULT_BACKUP_DIR) -> dict[str, Any]:
    """One population of 8.7 as sorted project ids plus how it was built."""
    if name not in POPULATIONS:
        raise ValueError(f"unknown population {name!r}; one of {', '.join(POPULATIONS)}")
    data = data or BackupData(backup_dir)
    golden = list(GOLDEN_IDS)
    non_holdout = [g for g in golden if g not in HOLDOUT_IDS]
    if name == "golden":
        return {"ids": golden, "n": len(golden), "rule": "drawings + BOQ + estimate in the backup (8.1)"}
    if name == "lopo":
        ids = []
        failing = []
        for pid in non_holdout:
            t = g3_truth(data, pid)
            if t["g3_sent"]["total"] is None or t["g3_verify"]["sum"] is None:
                continue
            if t["g3_verify"]["matches_sent"]:
                ids.append(pid)
            else:
                failing.append(pid[:8])
        return {"ids": sorted(ids), "n": len(ids), "failed_reconcile": failing, "rule": "golden with a committed Verify and a sent proposal, minus holdout, minus Verify sum != latest sent within $1"}
    if name == "estimate":
        rows = [r for r in data.table("project_files") if r.get("category") == "estimate" and str(r.get("filename", "")).lower().endswith((".xlsx", ".xlsm"))]
        projects = sorted({str(r["project_id"]) for r in rows})
        return {"ids": [p for p in projects if p not in HOLDOUT_IDS], "n_files": len(rows), "n_projects_all": len(projects), "rule": "every estimate xlsx in the backup (parser dry run covers all); library minus holdout"}
    if name == "wage":
        rows = [p for p in data.table("projects") if p.get("wage_type") and not str(p.get("name", "")).startswith("ZZ") and "TEST" not in str(p.get("name", "")).upper()]
        ids = sorted(str(p["id"]) for p in rows)
        return {"ids": [p for p in ids if p not in HOLDOUT_IDS], "holdout": [p for p in ids if p in HOLDOUT_IDS], "n_prevailing": sum(1 for p in rows if p.get("wage_type") == "prevailing_wage"), "rule": "projects with wage_type set, minus ZZ and TEST names; holdout only in the report column"}
    if name == "markup":
        rows = [r for r in data.table("project_files") if r.get("category") == "markup"]
        projects = sorted({str(r["project_id"]) for r in rows})
        return {"ids": [p for p in projects if p not in HOLDOUT_IDS], "n_files": len(rows), "rule": "projects with a markup file, minus holdout"}
    if name == "bands":
        est = population("estimate", data)
        return {"ids": est["ids"], "rule": "estimate population, one book each (8.8), minus budget, ROM and labor-only books (WS7 filter), devices >= 20 for per-device metrics; LOPO in eval and live sweeps"}
    if name == "r3":
        from app.services.plans_reader_score import parse_estimator_boq  # local: heavy

        count = 0
        projects: set[str] = set()
        for pid in non_holdout:
            boq = [r for r in data.files(pid, "boq") if str(r.get("filename", "")).lower().endswith((".xlsx", ".xlsm"))]
            if not boq:
                continue
            try:
                rows = parse_estimator_boq(data.file_path(boq[-1]).read_bytes())
            except Exception:  # noqa: BLE001
                continue
            for r in rows:
                d = r.description.upper()
                if r.unit == "EA" and not r.is_accessory and R3_MATCH_RE.search(d) and not R3_EXCLUDE_RE.search(d):
                    count += 1
                    projects.add(pid)
        return {"ids": sorted(projects), "n_rows": count, "rule": "EA rows in golden estimator BOQs matching LINEAR|STRIP|TAPE|TRACK|UNDERCAB|COVE and not HEAD|END CAP|<n> FT"}
    if name == "tagged_training":
        pool = sorted(TIER_IDS["pool"] | {resolve_case_project("7154")})
        return {"ids": pool, "rule": "golden pool projects (not core, not holdout; 7154 included) whose WS3 survey class is tagged; WS3 writes the split to manifest/ws3_split.json"}
    return {"ids": [resolve_case_project(c) for c in OLD_PIPELINE_BUNDLES], "bundles": OLD_PIPELINE_BUNDLES, "rule": "7154 weekend bundle; the 7172 and 7177 dev runs of WS0 step 7"}


def build_manifest(
    backup_dir: Path | str = DEFAULT_BACKUP_DIR,
    *,
    eval_root_dir: Path | str | None = None,
    count_pages: bool = True,
    populations: Iterable[str] = ("golden", "lopo", "estimate", "wage", "markup", "old_pipeline_bundles"),
) -> Manifest:
    """Deterministic manifest over the backup (read-only)."""
    data = BackupData(backup_dir)
    root = Path(eval_root_dir) if eval_root_dir is not None else eval_root()
    cases: list[Case] = []
    for tier, rows in TIER_CASES.items():
        for case_id, prefix, _name in rows:
            cases.append(build_case(data, case_id, prefix, tier, eval_root_dir=root, count_pages=count_pages))
    answer_sha: dict[str, str] = {}
    for c in cases:
        for a in c.answers:
            if a.sha256 and not a.input_copy:
                answer_sha.setdefault(a.sha256, f"{c.case_id}:{a.filename}")
    # Answer hashes of every golden project (not only the 12 cases), minus
    # bytes that are also a quantity input of the same project.
    files_by_project: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in data.table("project_files"):
        files_by_project[str(row.get("project_id"))].append(row)
    case_projects = {c.project_id for c in cases}
    for pid in GOLDEN_IDS:
        if pid in case_projects:
            continue
        rows = files_by_project.get(pid, ())
        project_hashes = {str(r["id"]): (sha256_file(data.file_path(r)) if data.file_path(r).exists() else "") for r in rows}
        input_hashes = {project_hashes[str(r["id"])] for r in rows if r.get("category") in QUANTITY_INPUT_CATEGORIES}
        for row in rows:
            sha = project_hashes[str(row["id"])]
            if row.get("category") in ANSWER_CATEGORIES and sha and sha not in input_hashes:
                answer_sha.setdefault(sha, f"{pid[:8]}:{row.get('filename')}")
    pops = {name: population(name, data) for name in populations}
    notes = [
        "Tiers are fixed by spec 8.1; nothing is swapped in from the pool.",
        "Holdout projects never feed mining, libraries, thresholds, prompts or adjudications; the holdout sweep runs once at go/no-go.",
        "Big sheets = pages of at least 24 x 18 in in drawing, electrical_drawing and addendum files; spec pages = pages of specification files.",
    ]
    for c in cases:
        for i in c.inputs:
            if i.misfiled:
                notes.append(f"{c.case_id}: {i.filename} is misfiled and excluded: {i.note}")
    return Manifest(
        schema=MANIFEST_SCHEMA,
        built_at=now_pacific_iso(),
        backup_dir=str(data.backup_dir),
        cases=cases,
        golden_ids=list(GOLDEN_IDS),
        holdout_ids=sorted(HOLDOUT_IDS),
        answer_sha256=answer_sha,
        populations=pops,
        notes=notes,
    )


def write_manifest(manifest: Manifest, root: Path | str | None = None) -> Path:
    path = manifest_path(root) / "manifest" / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest.to_dict(), indent=2, default=str) + "\n")
    return path


def load_manifest(root: Path | str | None = None) -> Manifest:
    path = manifest_path(root) / "manifest" / "manifest.json"
    return Manifest.from_dict(json.loads(path.read_text()))


def cases(tier: str | None = None, root: Path | str | None = None) -> list[Case]:
    manifest = load_manifest(root)
    return [c for c in manifest.cases if tier is None or c.tier == tier]


def answer_hashes(root: Path | str | None = None) -> dict[str, str]:
    try:
        return dict(load_manifest(root).answer_sha256)
    except FileNotFoundError:
        return {}


__all__ = [
    "MANIFEST_SCHEMA", "TIERS", "STAGES", "PURPOSES", "ANSWER_CATEGORIES", "QUANTITY_INPUT_CATEGORIES",
    "PRICING_INPUT_CATEGORIES", "ANSWER_NAME_RE", "TIER_CASES", "CASE_TIER", "CASE_PREFIX", "GOLDEN_IDS",
    "HOLDOUT_IDS", "TIER_IDS", "CORE_DROP_ORDER", "OLD_PIPELINE_BUNDLES", "CASE_NOTES", "POPULATIONS",
    "BlindViolation", "CaseInput", "CaseAnswer", "Case", "Manifest", "tier_of", "resolve_case_project",
    "excluded_projects", "excluded_project_ids_sha256", "blind_guard", "is_big_sheet", "build_case",
    "population", "build_manifest", "write_manifest", "load_manifest", "cases", "answer_hashes",
    "eval_root", "to_pacific_iso",
]
