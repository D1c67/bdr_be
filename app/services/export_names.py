"""Short, path-safe names for ZIP exports.

Users save exports into already deep folders (OneDrive project trees), and
Windows refuses to open anything whose full path passes 260 characters. Every
name inside an export archive therefore goes through `short_name`, which:

* shrinks a full job number (`26.9.7126`, `26.9.7126B`) to its last 4 digits,
* abbreviates the construction terms that show up in almost every bid set
  (`E-Sheets` -> `E-`, `Low Voltage` -> `LV`, `Specifications` -> `Spec`, ...),
* compacts the splitter's page ranges (`(pages 12-40)` -> `p12-40`).

`fit` then caps a name's length while keeping its extension, and `fit_path`
caps a whole archive path. Stored filenames are never touched; this only
shapes what lands inside the ZIP.
"""

import re

# Per-component and whole-path budgets inside the archive. 120 leaves roughly
# 140 characters of the 260 Windows limit for the user's own folders plus the
# folder "Extract All" creates from the zip name.
FOLDER_MAX = 40
FILE_MAX = 80
PATH_MAX = 120
_STEM_MIN = 20

# A BDR job number, YY.M.NNNN with an optional letter suffix (26.9.7126B).
_JOB_NUMBER = re.compile(r"\b\d{2}\.\d{1,2}\.(\d{4})([A-Za-z])?\b")

# Order matters: multi-word phrases before the single words inside them.
_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(p, re.IGNORECASE), r)
    for p, r in [
        # Splitter page ranges.
        (r"\(\s*pages?\s+(\d+)\s*-\s*(\d+)\s*\+\s*cover\s+sheets?\s*\)", r"p\1-\2+cover"),
        (r"\(\s*pages?\s+(\d+)\s*-\s*(\d+)\s*\)", r"p\1-\2"),
        (r"\(\s*page\s+(\d+)\s*\)", r"p\1"),
        (r"\bGeneral\s*/\s*Cover\s+Sheets?\b", "Gen-Cover"),
        (r"\bMechanical,?\s+Electrical,?\s+(?:and|&)\s+Plumbing\b", "MEP"),
        (r"\bRequest\s+for\s+Proposals?\b", "RFP"),
        (r"\bRequest\s+for\s+(?:Quotes?|Quotations?)\b", "RFQ"),
        (r"\bRequest\s+for\s+Information\b", "RFI"),
        (r"\bInvitation\s+to\s+Bid\b", "ITB"),
        (r"\bInstructions\s+to\s+Bidders\b", "Instr to Bidders"),
        (r"\bLow[\s-]+Voltage\b", "LV"),
        (r"\bSwitch[\s-]*Gear\b", "SG"),
        (r"\bFire\s+Protection\b", "FP"),
        (r"\bFire\s+Alarm\b", "FA"),
        (r"\b(?:Single|One)[\s-]+Line(?:\s+Diagrams?)?\b", "SLD"),
        (r"\bElectric\s+Vehicle\b", "EV"),
        (r"\bCover\s+Sheets?\b", "Cover"),
        (r"\bProject\s+Manual\b", "Proj Manual"),
        # Sheet sets: "E-Sheets", "E Sheets", "FA-Sheets" -> "E-", "FA-".
        (r"\b([A-Za-z]{1,2})[\s-]?Sheets?\b", "\\1-"),
        (r"\bSpecifications?\b", "Spec"),
        (r"\bDrawings\b", "Dwgs"),
        (r"\bDrawing\b", "Dwg"),
        (r"\bElectrical\b", "Elec"),
        (r"\bMechanical\b", "Mech"),
        (r"\bArchitectural\b", "Arch"),
        (r"\bStructural\b", "Struct"),
        (r"\bPlumbing\b", "Plumb"),
        (r"\bGeneral\b", "Gen"),
        (r"\bAddend(?:um|a)\b", "Add"),
        (r"\bGeotechnical\b", "Geotech"),
        (r"\bReports?\b", "Rpt"),
        (r"\bDivision\b", "Div"),
        (r"\bPhotovoltaic\b", "PV"),
        (r"\bUnderground\b", "UG"),
        (r"\bGrounding\b", "Gnd"),
        (r"\bLighting\b", "Ltg"),
        (r"\bTransformers?\b", "Xfmr"),
        (r"\bPanelboards?\b", "Pnl"),
        (r"\bDistribution\b", "Dist"),
        (r"\bTelecommunications?\b", "Telecom"),
        (r"\bCommunications?\b", "Comm"),
        (r"\bSchedules?\b", "Sched"),
        (r"\bDetails\b", "Dtls"),
        (r"\bElevations\b", "Elev"),
        (r"\bRevisions?\b", "Rev"),
        (r"\bDemolition\b", "Demo"),
        (r"\bExisting\b", "Exist"),
    ]
]


def short_job_number(number: str | None) -> str | None:
    """The last 4 digits of a job number (letter suffix kept: 7126B), or None
    when there are fewer than 4 digits to take."""
    if not number:
        return None
    m = _JOB_NUMBER.search(number)
    if m:
        return m.group(1) + (m.group(2) or "").upper()
    digits = re.sub(r"\D", "", number)
    return digits[-4:] if len(digits) >= 4 else None


def short_name(name: str) -> str:
    """Job numbers to 4 digits, common terms abbreviated, whitespace tidied."""
    out = _JOB_NUMBER.sub(lambda m: m.group(1) + (m.group(2) or "").upper(), name)
    for pattern, repl in _RULES:
        out = pattern.sub(lambda m, r=repl: _upper_sheet(m, r), out)
    out = re.sub(r"\s+", " ", out)
    out = re.sub(r"\s+\.", ".", out)  # "E- .pdf" leftovers
    return out.strip()


def _upper_sheet(m: re.Match[str], repl: str) -> str:
    # The sheet-set rule keeps the discipline letters, uppercased (e- -> E-).
    if repl == "\\1-":
        return m.group(1).upper() + "-"
    return m.expand(repl)


def _split_ext(name: str) -> tuple[str, str]:
    stem, dot, ext = name.rpartition(".")
    # A real extension is short and has no spaces ("Rev 2.1 plans" is no ext).
    if dot and stem and 0 < len(ext) <= 5 and " " not in ext:
        return stem, "." + ext
    return name, ""


def fit(name: str, max_len: int) -> str:
    """Cap `name` at `max_len` characters, trimming the stem and keeping the
    extension. Never ends on a space, dot or dangling separator."""
    if len(name) <= max_len:
        return name
    stem, ext = _split_ext(name)
    keep = max(max_len - len(ext), 1)
    stem = stem[:keep].rstrip(" .-_,(")
    return (stem or "file") + ext


def fit_path(folders: list[str], filename: str, max_len: int = PATH_MAX) -> str:
    """Join capped folders and a capped filename; if the whole path is still
    over `max_len`, shorten the filename (down to a floor), then the folders."""
    parts = [fit(f, FOLDER_MAX) for f in folders]
    name = fit(filename, FILE_MAX)
    joined = "/".join(parts + [name])
    over = len(joined) - max_len
    if over > 0:
        stem, ext = _split_ext(name)
        room = max(len(stem) - over, _STEM_MIN)
        name = fit(name, room + len(ext))
        joined = "/".join(parts + [name])
    # Deep trees: trim the folders from the outside in until it fits.
    i = 0
    while len(joined) > max_len and i < len(parts):
        excess = len(joined) - max_len
        parts[i] = fit(parts[i], max(len(parts[i]) - excess, 8))
        joined = "/".join(parts + [name])
        i += 1
    return joined


def flat_name(folders: list[str], filename: str, max_len: int = PATH_MAX) -> str:
    """One-folder variant: the folders become a " - " prefix on the filename,
    so a flat export still says where each file came from."""
    prefix = " - ".join(fit(f, FOLDER_MAX) for f in folders if f)
    name = f"{prefix} - {filename}" if prefix else filename
    return fit(name, max_len)
