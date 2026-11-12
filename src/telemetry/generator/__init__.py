"""Synthetic GPON/HFC telemetry with labeled faults (spec §4)."""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ..config import GeneratorConfig
from .behavior import simulate_site
from .faults import apply_offline_signal, inject_collection_gaps, inject_site_faults, inject_site_outage
from .topology import build_topology
from .writer import data_quality_faults, set_mtimes, write_rows, write_site


@dataclass
class GenerateResult:
    files: int
    rows: int
    incidents: dict[str, int]
    data_quality: dict[str, int]
    gaps: int


def generate(cfg: GeneratorConfig, out: Path) -> GenerateResult:
    """Write a complete synthetic dataset under ``out``. Same config and seed -> identical bytes."""
    cfg.validate()
    inbox, truth = out / "inbox", out / "ground_truth"
    for d in (inbox, truth):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)

    rng = np.random.default_rng(cfg.seed)
    topo = build_topology(cfg)
    topo.to_csv(out / "topology.csv", index=False, lineterminator="\n")

    site_ids = list(dict.fromkeys(topo["site_id"]))
    # one site outage per technology first, then any extra ones at random
    by_tech = {t: [s for s in site_ids if topo.loc[topo.site_id == s, "tech"].iloc[0] == t] for t in ("gpon", "hfc")}
    outage_sites = [by_tech["gpon"][int(rng.integers(len(by_tech["gpon"])))],
                    by_tech["hfc"][int(rng.integers(len(by_tech["hfc"])))]][: cfg.site_outages]
    while len(outage_sites) < cfg.site_outages:
        outage_sites.append(site_ids[int(rng.integers(len(site_ids)))])

    incidents, gaps, files, rows = [], [], [], 0
    for sid in site_ids:
        site_rng = np.random.default_rng([cfg.seed, int(sid[1:])])  # independent stream per site
        site = simulate_site(cfg, topo[topo.site_id == sid], site_rng)
        site_inc = inject_site_faults(cfg, site, site_rng)
        for _ in range(outage_sites.count(sid)):
            site_inc.append(inject_site_outage(site, site_rng))
        busy = [(i.start, i.end) for i in site_inc if i.kind in ("port_outage", "site_outage", "degradation")]
        gaps += inject_collection_gaps(cfg, site, site_rng, busy)
        apply_offline_signal(site)
        files += write_site(cfg, site, inbox)
        rows += int(site.collected.sum()) * len(site.devices)
        incidents += site_inc

    deliveries, dq = data_quality_faults(cfg, files, inbox, rng)
    write_rows(truth / "incidents.csv", [i.row() for i in sorted(incidents, key=lambda i: (i.start, i.site_id, i.kind))],
               ["kind", "scope", "site_id", "port_id", "device_id", "start", "end"])
    write_rows(truth / "collection_gaps.csv", [{"site_id": g.site_id, "start": g.start, "end": g.end} for g in gaps],
               ["site_id", "start", "end"])
    write_rows(truth / "data_quality.csv", dq, ["fault", "file_name", "original_file", "detail"])
    write_rows(truth / "deliveries.csv", [{"seq": i + 1, **d} for i, d in enumerate(deliveries)],
               ["seq", "delivered_at", "file_name"])
    set_mtimes(inbox, deliveries)

    count = lambda xs, key: {k: sum(1 for x in xs if key(x) == k) for k in sorted({key(x) for x in xs})}  # noqa: E731
    return GenerateResult(files=len(list(inbox.glob("*.csv"))), rows=rows,
                          incidents=count(incidents, lambda i: i.kind),
                          data_quality=count(dq, lambda d: d["fault"]), gaps=len(gaps))
