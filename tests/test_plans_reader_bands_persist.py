"""plans_reader_bands.persist_bands / load_active_bands against a fake
Supabase client (migration MF, `ai_history_bands`; build spec WS7 wave 2).
Synthetic numbers only."""

from __future__ import annotations

import datetime as dt

from app.services import plans_reader_bands as pb

from tests.ws7_fake_sb import FakeSB

BUILT = dt.datetime(2026, 10, 21, 9, 0, tzinfo=pb.COMPANY_TZ)


def _summaries(n: int = 30, *, scale: float = 1.0) -> list[pb.EstimateSummary]:
    out = []
    for i in range(n):
        devices = 40 + i
        out.append(pb.EstimateSummary(
            project_id=f"p{i:02d}", project_type="other", devices=devices,
            hours=devices * (2.0 + 0.1 * i) * scale, branch_wire_ft=devices * (30 + i),
            emt_ft=devices * (10 + 0.5 * i), emt34_ft=devices * (8 + 0.3 * i), bid_usd=devices * (500 + 10 * i),
        ))
    return out


def _bands(scale: float = 1.0) -> pb.HistoryBands:
    return pb.build_bands(_summaries(scale=scale), built_at=BUILT, excluded_project_ids=["holdout-1"])


def test_persist_upserts_one_row_per_metric_and_stratum():
    sb = FakeSB()
    bands = _bands()
    n = pb.persist_bands(sb, bands)
    rows = sb.tables["ai_history_bands"]
    assert n == len(bands.bands) == len(rows)
    upserts = [c for c in sb.calls if c[1] == "upsert"]
    assert upserts and all(c[3] == "band_version,metric,stratum" for c in upserts)
    keys = {(r["band_version"], r["metric"], r["stratum"]) for r in rows}
    assert len(keys) == len(rows)
    row = next(r for r in rows if r["metric"] == "hours_per_device" and r["stratum"] == "building")
    band = bands.band("hours_per_device", "building")
    assert row["n"] == band.n == 30
    assert round(row["p50"], 6) == round(band.quantiles()["p50"], 6)
    assert len(row["values"]) == 30 and {"project_id", "value"} <= set(row["values"][0])
    assert row["manifest_sha256"] == pb.manifest_sha256(bands)
    assert row["excluded_project_ids"] == ["holdout-1"]
    assert row["lopo"]["critical_scope"] == "extreme"
    assert all(r["active"] for r in rows)


def test_null_quantiles_are_omitted_from_the_row():
    sb = FakeSB()
    lonely = pb.build_bands(
        [pb.EstimateSummary(project_id="z1", project_type="roadway", devices=50, hours=None,
                            branch_wire_ft=0.0, emt_ft=0.0, emt34_ft=0.0)], built_at=BUILT,
    )
    pb.persist_bands(sb, lonely)
    for row in sb.tables["ai_history_bands"]:
        if row["n"] == 0:
            assert "p10" not in row and "p90" not in row
            assert row["zeros"] == ["z1"]


def test_activate_switches_off_older_versions_and_load_rebuilds():
    sb = FakeSB()
    old = _bands()
    new = pb.build_bands(_summaries(scale=1.3), built_at=BUILT + dt.timedelta(days=1))
    assert old.band_version != new.band_version
    pb.persist_bands(sb, old)
    pb.persist_bands(sb, new)
    active_versions = {r["band_version"] for r in sb.tables["ai_history_bands"] if r["active"]}
    assert active_versions == {new.band_version}
    loaded = pb.load_active_bands(sb)
    assert loaded is not None and loaded.band_version == new.band_version
    assert set(loaded.bands) == set(new.bands)
    for key, band in new.bands.items():
        got = loaded.bands[key]
        assert got.values == tuple((p, round(v, 6)) for p, v in band.values)
        assert got.lopo.critical_supported == band.lopo.critical_supported
    # check_bands answers the same on the loaded set
    probe = pb.EstimateSummary(project_id="probe", project_type="ti", devices=60, hours=600.0,
                               branch_wire_ft=0.0, emt_ft=900.0, emt34_ft=700.0)
    a = [(c.metric, c.position, c.severity) for c in pb.check_bands(probe, new)]
    b = [(c.metric, c.position, c.severity) for c in pb.check_bands(probe, loaded)]
    assert a == b
    by_version = pb.load_active_bands(sb, band_version=old.band_version)
    assert by_version is not None and by_version.band_version == old.band_version


def test_persist_without_activate_keeps_the_current_active_version():
    sb = FakeSB()
    first = _bands()
    pb.persist_bands(sb, first)
    second = pb.build_bands(_summaries(scale=0.8), built_at=BUILT + dt.timedelta(days=2))
    pb.persist_bands(sb, second, activate=False)
    # The fake has no column defaults: a row inserted without `active` reads
    # as missing here and as false (the MF default) in the database.
    assert {r["band_version"] for r in sb.tables["ai_history_bands"] if r.get("active")} == {first.band_version}


def test_load_active_bands_missing_table_reads_as_none():
    assert pb.load_active_bands(FakeSB(fail_tables={"ai_history_bands"})) is None
    assert pb.load_active_bands(FakeSB()) is None


def test_repersisting_the_active_version_without_activate_keeps_it_active():
    sb = FakeSB()
    bands = _bands()
    pb.persist_bands(sb, bands)
    pb.persist_bands(sb, bands, activate=False)  # a backfill or a --persist rerun
    rows = sb.tables["ai_history_bands"]
    assert rows and all(r["active"] for r in rows)
    assert pb.load_active_bands(sb) is not None
    upserts = [c for c in sb.calls if c[1] == "upsert"]
    assert all("active" not in row for c in upserts for row in c[2])  # the flag is never upserted


def test_a_new_version_persisted_without_activate_starts_inactive():
    sb = FakeSB()
    bands = _bands()
    pb.persist_bands(sb, bands, activate=False)
    rows = sb.tables["ai_history_bands"]
    assert rows and not any(r.get("active") for r in rows)  # the column default (false) on insert
    assert pb.band_rows(bands)[0]["active"] is False and "active" not in pb.band_rows(bands, active=None)[0]
