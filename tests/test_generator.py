"""M1 acceptance (spec §9): same seed -> byte-identical files; every fault kind present."""
import hashlib
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from telemetry.config import GeneratorConfig
from telemetry.generator import generate

SMALL = GeneratorConfig(seed=7, sites=2, ports_per_site=3, devices_per_port=8,
                        start=date(2026, 9, 28), days=4)  # crosses the month boundary


def tree_md5(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): hashlib.md5(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    out = tmp_path_factory.mktemp("run")
    return generate(SMALL, out), out


def test_same_seed_gives_byte_identical_output(tmp_path, run):
    _, out = run
    generate(SMALL, tmp_path)
    assert tree_md5(out) == tree_md5(tmp_path)


def test_different_seed_changes_output(tmp_path, run):
    _, out = run
    generate(GeneratorConfig(**{**SMALL.__dict__, "seed": 8}), tmp_path)
    assert tree_md5(out) != tree_md5(tmp_path)


def test_every_fault_kind_is_present(run):
    res, out = run
    assert set(res.incidents) == {"device_drop", "port_outage", "site_outage", "degradation"}
    dq = set(pd.read_csv(out / "ground_truth" / "data_quality.csv")["fault"])
    assert dq == {"late_file", "out_of_order", "duplicate_file", "renamed_duplicate",
                  "malformed_missing_column", "malformed_bad_value"}
    assert len(pd.read_csv(out / "ground_truth" / "collection_gaps.csv")) >= 1


def test_file_names_and_schema(run):
    _, out = run
    dq = pd.read_csv(out / "ground_truth" / "data_quality.csv")
    malformed = set(dq.loc[dq.fault.str.startswith("malformed"), "file_name"])
    for f in (out / "inbox").glob("*.csv"):
        tech, site, stamp = f.stem.split("_")
        assert tech in ("gpon", "hfc") and site.startswith("s") and len(stamp) == 12
        if f.name not in malformed:
            df = pd.read_csv(f)
            assert list(df.columns) == ["device_id", "polled_at", "status", "signal"]
            assert set(df.status.unique()) <= {0, 1}
            assert df.loc[df.status == 0, "signal"].isna().all()       # null when offline
            assert df.loc[df.status == 1, "signal"].notna().all()


def test_renamed_duplicate_has_identical_bytes(run):
    _, out = run
    dq = pd.read_csv(out / "ground_truth" / "data_quality.csv").set_index("fault")
    r = dq.loc["renamed_duplicate"]
    inbox = out / "inbox"
    assert (inbox / r.file_name).read_bytes() == (inbox / r.original_file).read_bytes()
    assert r.file_name != r.original_file


def test_duplicate_is_delivered_twice(run):
    _, out = run
    dq = pd.read_csv(out / "ground_truth" / "data_quality.csv").set_index("fault")
    deliveries = pd.read_csv(out / "ground_truth" / "deliveries.csv")
    assert (deliveries.file_name == dq.loc["duplicate_file", "file_name"]).sum() == 2


def test_late_file_belongs_to_september_and_arrives_in_october(run):
    _, out = run
    dq = pd.read_csv(out / "ground_truth" / "data_quality.csv").set_index("fault")
    late = dq.loc["late_file", "file_name"]
    d = pd.read_csv(out / "ground_truth" / "deliveries.csv").set_index("file_name")
    delivered = pd.Timestamp(d.loc[late, "delivered_at"]).tz_convert(SMALL.tz)
    polls = pd.to_datetime(pd.read_csv(out / "inbox" / late).polled_at).dt.tz_convert(SMALL.tz)
    assert (polls.dt.month == 9).all() and delivered.month == 10


def _site_polls(out: Path, site: str) -> pd.DataFrame:
    dq = pd.read_csv(out / "ground_truth" / "data_quality.csv")
    skip = set(dq.loc[dq.fault.isin(["renamed_duplicate", "malformed_missing_column", "malformed_bad_value"]), "file_name"])
    parts = [pd.read_csv(f) for f in sorted((out / "inbox").glob(f"*_{site}_*.csv")) if f.name not in skip]
    df = pd.concat(parts)
    df["polled_at"] = pd.to_datetime(df.polled_at)
    return df


def test_port_outage_takes_every_device_of_the_port_offline(run):
    _, out = run
    inc = pd.read_csv(out / "ground_truth" / "incidents.csv", parse_dates=["start", "end"])
    po = inc[inc.kind == "port_outage"].iloc[0]
    df = _site_polls(out, po.site_id)
    port_prefix = po.port_id.replace("-", "-")  # e.g. s01-p02
    inside = df[df.device_id.str.contains(port_prefix.split("-")[1]) &
                df.device_id.str.contains(f"-{po.site_id}-") &
                (df.polled_at >= po.start) & (df.polled_at < po.end)]
    assert len(inside) > 0 and (inside.status == 0).all()


def test_collection_gap_has_no_rows(run):
    _, out = run
    gap = pd.read_csv(out / "ground_truth" / "collection_gaps.csv", parse_dates=["start", "end"]).iloc[0]
    df = _site_polls(out, gap.site_id)
    assert not ((df.polled_at >= gap.start) & (df.polled_at < gap.end)).any()


def test_default_config_matches_spec():
    cfg = GeneratorConfig()
    n_devices = cfg.sites * cfg.ports_per_site * cfg.devices_per_port
    assert n_devices == 1536
    assert cfg.polls_per_device == 288 * 21
    assert cfg.start == date(2026, 9, 21)
