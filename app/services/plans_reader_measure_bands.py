"""Measurement bands (WS3 step B9; docs/AI_ESTIMATING_BUILD_SPEC.md 6 WS3
"plans_reader_measure_bands.py (mined constant)", 5.7 and 8.7 bands
population, leave-one-project-out in eval and live sweeps).

The density tier (devices x band median) and the band checks of the measure
stage read the ACTIVE bands file: the latest `EVAL_ROOT/bands/hb*.json`
through `plans_reader_bands.latest_bands_path` and `load_bands`. Each
quantile is recomputed from the file's per-project values with
`eval_manifest.excluded_projects("calibration", case_id)` left out when the
run carries an eval case id (LOPO), and the exclusion hash is recorded in
the measure output (`bands.excluded_project_ids_sha256`). The hard-coded
blocks below are the LAST fallback only (no bands file, or an unreadable
one); using them logs a warning and sets `bands.warning` in the output.
`load_branch_bands` builds a `BranchBandSet`; `use(bandset)` makes it the
current set for the module functions (`band`, `band_position`,
`density_per_device`, `stratum_for`), which `plans_reader_routes`
reads without knowing about files.

Numbers only, no prices. Every value here was mined from the production
backup on disk with the holdout projects excluded through
`eval_manifest.excluded_projects("calibration")`; the provenance of each
block is recorded next to it so the owner can re-derive it. Nothing in this
module changes a code default on its own: `plans_reader_routes.Conventions`
reads the settings, and `HOMERUN_MEDIAN_FT` only replaces the home-run
allowance default once the owner signs off the calibrated conventions
version (spec 5.7, `plans_reader_measure_conventions_version` -> `-b`).

Fallback blocks:

- BRANCH_BANDS: the WS7 history bands `hb1-20261021-bbd6157d` (estimator
  basis, 8.7 bands population, holdout excluded, LOPO in sweeps) for the
  three branch metrics the measure stage checks its own result against and
  uses as the density tier (devices x band median): branch wire FT per
  device (#12 and #10, pre-waste), EMT FT per device (every size,
  pre-waste) and branch wire per 3/4" EMT. Strata `building`, `ti`, `all`.
- AWG10_SHARE (always this constant; the bands file has no #10 split, so
  it is not LOPO, recorded as `awg10_share_source` in the output): the #10 share of branch wire (#10 / (#12 + #10) FT) over the
  estimate books of the backup (one book per project, holdout excluded):
  median 0.237, p25 0.0, p75 0.613 over 46 projects with branch wire. The
  density tier splits the wire total with the median.
- WIRE_TO_CONDUIT_RATIO: (#12 + #10) / (1/2" + 3/4" EMT) median 3.00 over 41
  projects (the G3 convention "one circuit per conduit, H + N + G").
- HOMERUN_MEDIAN_FT: the golden median home-run length per circuit from the
  B9 calibration run over the tagged training split (None until that run
  has been recorded here; `plans_reader_measure_eval.py --calibrate` prints
  the value and the owner signs it off).
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterable, Iterator
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

BANDS_SOURCE = "hb1-20261021-bbd6157d"
BANDS_BUILT_AT = "2026-10-21T19:41:33-07:00"
MINED_AT = "2026-10-22"
EXCLUSION_PURPOSE = "calibration"

# metric -> stratum -> {n, p10, p25, p50, p75, p90}
BRANCH_BANDS: dict[str, dict[str, dict[str, float]]] = {
    "branch_wire_ft_per_device": {
        "building": {"n": 29, "p10": 22.335, "p25": 34.785, "p50": 45.978, "p75": 59.799, "p90": 99.09},
        "ti": {"n": 7, "p10": 39.81, "p25": 43.303, "p50": 45.978, "p75": 59.674, "p90": 73.79},
        "all": {"n": 41, "p10": 10.769, "p25": 28.41, "p50": 42.291, "p75": 56.428, "p90": 95.317},
    },
    "emt_ft_per_device": {
        "building": {"n": 28, "p10": 6.361, "p25": 12.044, "p50": 15.997, "p75": 19.757, "p90": 31.685},
        "ti": {"n": 7, "p10": 13.438, "p25": 13.68, "p50": 15.501, "p75": 19.508, "p90": 24.314},
        "all": {"n": 35, "p10": 2.224, "p25": 7.521, "p50": 15.253, "p75": 19.129, "p90": 28.92},
    },
    "wire_per_emt34": {
        "building": {"n": 34, "p10": 2.974, "p25": 3.0, "p50": 3.068, "p75": 3.979, "p90": 5.332},
        "ti": {"n": 8, "p10": 2.963, "p25": 2.992, "p50": 3.051, "p75": 3.396, "p90": 7.393},
        "all": {"n": 40, "p10": 2.99, "p25": 3.0, "p50": 3.049, "p75": 4.075, "p90": 6.934},
    },
}

# #10 share of branch wire over the estimate books (46 projects, holdout excluded).
AWG10_SHARE: dict[str, float] = {"p25": 0.0, "p50": 0.237, "p75": 0.613, "n": 46}

# (#12 + #10) / branch EMT over the estimate books (41 projects, holdout excluded).
WIRE_TO_CONDUIT_RATIO: dict[str, float] = {"p50": 3.0, "n": 41}
WIRE_TO_CONDUIT_BAND: tuple[float, float] = (2.6, 3.4)

# Golden median home-run length per circuit (B9 calibration, owner sign-off).
# HOMERUN_MEDIAN_FT stays None (the 150 ft code default applies) until the
# owner signs off; the candidate is the median over 57 drawn home runs of
# the tagged training split (16 projects with branch truth, holdout
# excluded), `plans_reader_measure_eval.py --calibrate` on 2026-10-22,
# reports/measure_calibration.md.
HOMERUN_MEDIAN_FT: float | None = None
HOMERUN_MEDIAN_CANDIDATE_FT: float = 60.1

BUILDING_TYPES: frozenset[str] = frozenset({"ti", "other", "new_construction", "multi_family"})
MIN_STRATUM_N = 8
BRANCH_METRICS: tuple[str, ...] = ("branch_wire_ft_per_device", "emt_ft_per_device", "wire_per_emt34")
FALLBACK_SOURCE = f"constants:{BANDS_SOURCE}"


@dataclass(frozen=True)
class BranchBandSet:
    """The branch bands one measure build uses: metric -> stratum ->
    {n, p10, p25, p50, p75, p90}, from the active bands file (LOPO when a
    case id is given) or the fallback constants."""

    source: str
    from_file: bool
    bands: dict[str, dict[str, dict[str, float]]]
    case_id: str | None = None
    excluded_project_ids: tuple[str, ...] = ()
    excluded_project_ids_sha256: str | None = None
    warning: str | None = None
    awg10_share: dict[str, float] = field(default_factory=lambda: dict(AWG10_SHARE))
    path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "bands_source": self.source,
            "from_file": self.from_file,
            "bands_case_id": self.case_id,
            "excluded_project_ids_sha256": self.excluded_project_ids_sha256,
            "excluded_count": len(self.excluded_project_ids),
            "awg10_share_source": "constant (estimate books, holdout excluded, not LOPO)",
            "warning": self.warning,
        }


def fallback_bands(warning: str) -> BranchBandSet:
    """The hard-coded hb1 constants (last fallback), with the warning."""
    logger.warning("plans_reader_measure_bands: %s", warning)
    return BranchBandSet(source=FALLBACK_SOURCE, from_file=False, bands={m: {s: dict(q) for s, q in by.items()} for m, by in BRANCH_BANDS.items()}, warning=warning)


_FILE_CACHE: dict[tuple[str, float], Any] = {}


def _load_file(path: Path) -> Any:
    from app.services import plans_reader_bands

    key = (str(path), path.stat().st_mtime)
    if key not in _FILE_CACHE:
        _FILE_CACHE.clear()
        _FILE_CACHE[key] = plans_reader_bands.load_bands(path)
    return _FILE_CACHE[key]


def bands_from_history(history: Any, *, exclude: Iterable[str] = (), case_id: str | None = None, path: str | None = None) -> BranchBandSet:
    """A BranchBandSet from a `plans_reader_bands.HistoryBands`, every
    quantile recomputed with `exclude` left out of each band."""
    from app.services import eval_manifest

    drop = tuple(sorted({str(p) for p in exclude if p}))
    out: dict[str, dict[str, dict[str, float]]] = {}
    for metric in BRANCH_METRICS:
        for (m, stratum), band_obj in history.bands.items():
            if m != metric:
                continue
            b = band_obj.without(drop)
            q = b.quantiles()
            if not q:
                continue
            out.setdefault(metric, {})[stratum] = {"n": b.n, **{k: round(float(v), 4) for k, v in q.items()}}
    missing = [m for m in BRANCH_METRICS if "all" not in out.get(m, {})]
    if missing:
        raise ValueError(f"bands file {history.band_version} lacks the all stratum for {', '.join(missing)}")
    excluded = tuple(sorted(set(drop) | {str(p) for p in history.excluded_project_ids}))
    return BranchBandSet(
        source=str(history.band_version),
        from_file=True,
        bands=out,
        case_id=case_id,
        excluded_project_ids=excluded,
        excluded_project_ids_sha256=eval_manifest.excluded_project_ids_sha256(excluded),
        path=path,
    )


def load_branch_bands(*, case_id: str | None = None, root: Path | str | None = None, purpose: str = EXCLUSION_PURPOSE) -> BranchBandSet:
    """The active branch bands: the latest bands file under EVAL_ROOT/bands
    with `eval_manifest.excluded_projects(purpose, case_id)` left out of
    every band when `case_id` is set (LOPO in eval and live sweeps). The
    constants are the last fallback, with a warning."""
    try:
        from app.services import eval_manifest, plans_reader_bands

        path = plans_reader_bands.latest_bands_path(root)
        if path is None:
            return fallback_bands("no bands file under EVAL_ROOT/bands; hard-coded hb1 constants used (not LOPO)")
        history = _load_file(path)
        exclude = eval_manifest.excluded_projects(purpose, case_id) if case_id else frozenset()
        return bands_from_history(history, exclude=exclude, case_id=case_id, path=str(path))
    except Exception as exc:  # noqa: BLE001 - the constants keep the density tier alive
        return fallback_bands(f"bands file unreadable ({type(exc).__name__}: {str(exc)[:120]}); hard-coded hb1 constants used (not LOPO)")


_CURRENT: ContextVar[BranchBandSet | None] = ContextVar("plans_reader_measure_bands_current", default=None)


def current() -> BranchBandSet:
    """The set made current by `use`, else the active file without a case
    exclusion (loaded once per process and file change)."""
    got = _CURRENT.get()
    if got is not None:
        return got
    return load_branch_bands()


@contextlib.contextmanager
def use(bandset: BranchBandSet) -> Iterator[BranchBandSet]:
    """Make `bandset` the current set for the duration (per thread and
    task; the measure stage wraps one build in it)."""
    token = _CURRENT.set(bandset)
    try:
        yield bandset
    finally:
        _CURRENT.reset(token)


def stratum_for(project_type: str | None, bands: BranchBandSet | None = None) -> str:
    """`ti` for a TI project when its band has enough values, `building` for
    the building types (ti, other, new construction, multi family, casino_*),
    else `all`."""
    bs = bands or current()
    t = str(project_type or "").strip().lower()
    if t == "ti" and bs.bands.get("emt_ft_per_device", {}).get("ti", {}).get("n", 0) >= MIN_STRATUM_N:
        return "ti"
    if t in BUILDING_TYPES or t.startswith("casino_"):
        return "building"
    return "all"


def band(metric: str, stratum: str | None = None, bands: BranchBandSet | None = None) -> dict[str, float]:
    """The quantiles of one metric in one stratum (`all` when the stratum is
    unknown or thin)."""
    by = (bands or current()).bands[metric]
    s = stratum if stratum in by and by[stratum]["n"] >= MIN_STRATUM_N else "all"
    return dict(by[s])


def band_position(metric: str, value: float | None, stratum: str | None = None, bands: BranchBandSet | None = None) -> dict[str, Any]:
    """Where `value` sits against the band: below, in_band, above (p10 to
    p90), with the quantiles; `not_applicable` for None."""
    q = band(metric, stratum, bands)
    if value is None:
        return {"metric": metric, "value": None, "position": "not_applicable", **q}
    if value < q["p10"]:
        pos = "below"
    elif value > q["p90"]:
        pos = "above"
    else:
        pos = "in_band"
    return {"metric": metric, "value": round(float(value), 3), "position": pos, **q}


def density_per_device(stratum: str | None = None, bands: BranchBandSet | None = None) -> dict[str, float]:
    """The density tier's per-device medians: EMT FT and branch wire FT per
    new device, the #10 share and the wire-to-conduit ratio (the band's
    wire per 3/4" EMT median; the constant only on the fallback set)."""
    bs = bands or current()
    ratio = band("wire_per_emt34", stratum, bs)["p50"] if bs.from_file else WIRE_TO_CONDUIT_RATIO["p50"]
    return {
        "emt_ft_per_device": band("emt_ft_per_device", stratum, bs)["p50"],
        "wire_ft_per_device": band("branch_wire_ft_per_device", stratum, bs)["p50"],
        "awg10_share": bs.awg10_share["p50"],
        "wire_to_conduit": ratio,
    }


__all__ = [
    "BANDS_SOURCE",
    "BRANCH_BANDS",
    "AWG10_SHARE",
    "WIRE_TO_CONDUIT_RATIO",
    "WIRE_TO_CONDUIT_BAND",
    "HOMERUN_MEDIAN_FT",
    "HOMERUN_MEDIAN_CANDIDATE_FT",
    "BranchBandSet",
    "load_branch_bands",
    "bands_from_history",
    "fallback_bands",
    "current",
    "use",
    "stratum_for",
    "band",
    "band_position",
    "density_per_device",
]
