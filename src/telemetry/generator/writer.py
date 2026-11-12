"""Collector files, data-quality faults and ground truth on disk (spec §3.1, §4.3, §4.4).

Output layout (all under ``data/``, git-ignored):

    inbox/{tech}_{site_id}_{YYYYMMDDHHMM}.csv   collector files, one per site per batch (UTC)
    topology.csv                                device -> port/node -> site -> region, tech
    ground_truth/incidents.csv                  injected outages and degradations
    ground_truth/collection_gaps.csv            polls dropped by the collector (no data, not offline)
    ground_truth/data_quality.csv               duplicate, renamed, late, out-of-order, malformed files
    ground_truth/deliveries.csv                 arrival order of every file, including repeats

A folder cannot hold the same file name twice, so the plain "duplicate file"
fault lives in ``deliveries.csv`` as a second delivery of an existing file;
ingest can replay that manifest to reproduce arrival order and repeats.
"""
from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from ..config import GeneratorConfig
from .behavior import SiteArrays

COLUMNS = ["device_id", "polled_at", "status", "signal"]
ISO = "%Y-%m-%dT%H:%M:%SZ"


@dataclass
class BatchFile:
    name: str
    site_id: str
    batch_start: datetime
    batch_end: datetime


def file_name(tech: str, site_id: str, batch_start: datetime) -> str:
    return f"{tech}_{site_id}_{batch_start:%Y%m%d%H%M}.csv"


def batch_frame(site: SiteArrays, i0: int, i1: int) -> pd.DataFrame:
    """Rows for polls [i0, i1) that the collector actually made. The collector walks the
    devices in order within each poll, so each device answers a few seconds into the slot."""
    polls = np.arange(i0, i1)[site.collected[i0:i1]]
    n_dev = len(site.devices)
    if len(polls) == 0:
        return pd.DataFrame(columns=COLUMNS)
    offsets = (np.arange(n_dev) * 60 // n_dev).astype("timedelta64[s]")
    slot = site.poll_times[polls].tz_convert("UTC").tz_localize(None).to_numpy()
    when = (slot[None, :] + offsets[:, None]).T.ravel()                  # poll-major order
    status = site.online[:, polls].T.ravel().astype(np.int8)
    signal = site.signal[:, polls].T.ravel()
    return pd.DataFrame({
        "device_id": np.tile(site.devices["device_id"].to_numpy(), len(polls)),
        "polled_at": pd.to_datetime(when).strftime(ISO),
        "status": status,
        "signal": np.round(signal, 2),
    })


def write_csv(df: pd.DataFrame, path: Path) -> None:
    df.to_csv(path, index=False, float_format="%.2f", lineterminator="\n")


def write_site(cfg: GeneratorConfig, site: SiteArrays, inbox: Path) -> list[BatchFile]:
    per_batch = cfg.batch_hours * 60 // cfg.poll_minutes
    files = []
    for i0 in range(0, len(site.poll_times), per_batch):
        i1 = min(i0 + per_batch, len(site.poll_times))
        start = site.poll_times[i0].to_pydatetime()
        bf = BatchFile(file_name(site.tech, site.site_id, start), site.site_id, start,
                       start + timedelta(hours=cfg.batch_hours))
        write_csv(batch_frame(site, i0, i1), inbox / bf.name)
        files.append(bf)
    return files


def _shift_name(name: str, minutes: int) -> str:
    tech, site, stamp = name[:-4].split("_")
    t = datetime.strptime(stamp, "%Y%m%d%H%M") + timedelta(minutes=minutes)
    return f"{tech}_{site}_{t:%Y%m%d%H%M}.csv"


def data_quality_faults(cfg: GeneratorConfig, files: list[BatchFile], inbox: Path,
                        rng: np.random.Generator) -> tuple[list[dict], list[dict]]:
    """Build the delivery schedule and inject file-level faults. Returns (deliveries, faults)."""
    deliveries: list[dict] = []
    faults: list[dict] = []
    for f in files:  # normal arrival: a few minutes after the batch closes
        deliveries.append({"delivered_at": f.batch_end + timedelta(minutes=int(rng.integers(2, 21))),
                           "file_name": f.name})
    by_name = {d["file_name"]: d for d in deliveries}

    def pick(pred) -> BatchFile:
        candidates = [f for f in files if pred(f)]
        return candidates[int(rng.integers(len(candidates)))]

    zone = cfg.zone
    local_date = lambda f: f.batch_start.astimezone(zone).date()  # noqa: E731
    month_end = max((f for f in files), key=lambda f: f.batch_start)  # fallback
    sept30 = [f for f in files if local_date(f).month == 9 and local_date(f).day == 30]

    # Late file: a batch from 30 Sep (or the last day before a month boundary) arrives in October.
    late = (sept30[int(rng.integers(len(sept30)))] if sept30 else month_end)
    new_at = datetime.combine(late.batch_start.astimezone(zone).date() + timedelta(days=3),
                              datetime.min.time(), tzinfo=zone).astimezone(timezone.utc) + timedelta(hours=9)
    by_name[late.name]["delivered_at"] = new_at
    faults.append({"fault": "late_file", "file_name": late.name, "original_file": "",
                   "detail": f"batch of {local_date(late)} delivered on {new_at.astimezone(zone):%Y-%m-%d}"})

    # Out-of-order: a few batches arrive after the next batch of the same site.
    for _ in range(3):
        f = pick(lambda f: f.name != late.name)
        by_name[f.name]["delivered_at"] += timedelta(hours=cfg.batch_hours + int(rng.integers(1, 4)))
        faults.append({"fault": "out_of_order", "file_name": f.name, "original_file": "",
                       "detail": "arrives after the following batch of the same site"})

    # Duplicate file: the same file delivered again, later.
    dup = pick(lambda f: f.name != late.name)
    deliveries.append({"delivered_at": by_name[dup.name]["delivered_at"] + timedelta(hours=12), "file_name": dup.name})
    faults.append({"fault": "duplicate_file", "file_name": dup.name, "original_file": dup.name,
                   "detail": "same name and content delivered twice"})

    # Renamed duplicate: same bytes under a different name.
    src = pick(lambda f: f.name not in (late.name, dup.name))
    renamed = _shift_name(src.name, 1)
    (inbox / renamed).write_bytes((inbox / src.name).read_bytes())
    deliveries.append({"delivered_at": by_name[src.name]["delivered_at"] + timedelta(hours=1), "file_name": renamed})
    faults.append({"fault": "renamed_duplicate", "file_name": renamed, "original_file": src.name,
                   "detail": "identical content, different file name"})

    # Malformed files: extra deliveries that must be rejected without loading anything.
    for minutes, kind in ((2, "missing_column"), (3, "bad_value")):
        src = pick(lambda f: f.name not in (late.name, dup.name))
        df = pd.read_csv(inbox / src.name, dtype=str, keep_default_na=False)
        if kind == "missing_column":
            df = df.drop(columns=["status"])
            detail = "column 'status' missing"
        else:
            df.loc[df.index[:5], "status"] = "2"
            detail = "status = 2 in 5 rows (allowed: 0, 1)"
        name = _shift_name(src.name, minutes)
        df.to_csv(inbox / name, index=False, lineterminator="\n")
        deliveries.append({"delivered_at": by_name[src.name]["delivered_at"] + timedelta(minutes=30), "file_name": name})
        faults.append({"fault": f"malformed_{kind}", "file_name": name, "original_file": src.name, "detail": detail})

    deliveries.sort(key=lambda d: (d["delivered_at"], d["file_name"]))
    return deliveries, faults


def write_rows(path: Path, rows: list[dict], fields: list[str]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow({k: (v.strftime(ISO) if isinstance(v, datetime) else v) for k, v in r.items()})


def set_mtimes(inbox: Path, deliveries: list[dict]) -> None:
    """File modification time = last delivery time, so tools that sort by mtime see arrival order."""
    last = {}
    for d in deliveries:
        last[d["file_name"]] = d["delivered_at"]
    for name, when in last.items():
        ts = when.timestamp()
        os.utime(inbox / name, (ts, ts))
