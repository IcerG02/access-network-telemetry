"""Injected incidents with ground-truth labels (spec §4.3) and collection gaps (spec §4.4).

Every incident is applied to the site arrays and recorded with the columns of
``data/ground_truth/incidents.csv``: kind, scope, site_id, port_id, device_id, start, end.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime

import numpy as np

from ..config import GeneratorConfig
from .behavior import SiteArrays


@dataclass
class Incident:
    kind: str          # device_drop | port_outage | site_outage | degradation
    scope: str         # device | port | site
    site_id: str
    port_id: str
    device_id: str
    start: datetime    # UTC, inclusive
    end: datetime      # UTC, exclusive

    def row(self) -> dict:
        d = asdict(self)
        d["start"] = self.start.strftime("%Y-%m-%dT%H:%M:%SZ")
        d["end"] = self.end.strftime("%Y-%m-%dT%H:%M:%SZ")
        return d


@dataclass
class Gap:
    site_id: str
    start: datetime
    end: datetime


def _window(site: SiteArrays, rng: np.random.Generator, min_minutes: int, max_minutes: int,
            margin_polls: int = 0) -> tuple[int, int]:
    """Random [i0, i1) poll range of a duration in [min, max] minutes."""
    step = int((site.poll_times[1] - site.poll_times[0]).total_seconds() // 60)
    length = int(rng.integers(min_minutes, max_minutes + 1)) // step
    i0 = int(rng.integers(margin_polls, len(site.poll_times) - length))
    return i0, i0 + max(length, 1)


def _ts(site: SiteArrays, i: int) -> datetime:
    t = site.poll_times[i] if i < len(site.poll_times) else site.poll_times[-1] + (site.poll_times[1] - site.poll_times[0])
    return t.to_pydatetime()


def inject_site_faults(cfg: GeneratorConfig, site: SiteArrays, rng: np.random.Generator) -> list[Incident]:
    incidents: list[Incident] = []
    dev = site.devices

    for _ in range(cfg.device_drops_per_site):
        i = int(rng.integers(len(dev)))
        i0, i1 = _window(site, rng, 20, 6 * 60)
        site.online[i, i0:i1] = False
        incidents.append(Incident("device_drop", "device", site.site_id, dev.at[i, "port_id"],
                                  dev.at[i, "device_id"], _ts(site, i0), _ts(site, i1)))

    ports = dev["port_id"].unique()
    for k in range(cfg.port_outages_per_site):
        port = ports[int(rng.integers(len(ports)))]
        rows = np.flatnonzero(dev["port_id"].to_numpy() == port)
        degrade_polls = 0
        # the first port outage of each site is always preceded by degradation, so every
        # run has the pattern; the others follow ``degradation_share``
        if k == 0 or rng.random() < cfg.degradation_share:
            degrade_polls = int(rng.integers(3 * 60, 6 * 60 + 1)) // cfg.poll_minutes
        i0, i1 = _window(site, rng, 30, 4 * 60, margin_polls=degrade_polls)
        if degrade_polls:
            # signal drifts down for hours before the port goes dark: groundwork for anomaly detection
            d0 = i0 - degrade_polls
            drift = np.linspace(0.0, -float(rng.uniform(4.0, 7.0)), degrade_polls)
            site.signal[np.ix_(rows, np.arange(d0, i0))] += drift[None, :]
            incidents.append(Incident("degradation", "port", site.site_id, port, "",
                                      _ts(site, d0), _ts(site, i0)))
        site.online[np.ix_(rows, np.arange(i0, i1))] = False
        incidents.append(Incident("port_outage", "port", site.site_id, port, "", _ts(site, i0), _ts(site, i1)))
    return incidents


def inject_site_outage(site: SiteArrays, rng: np.random.Generator) -> Incident:
    i0, i1 = _window(site, rng, 45, 3 * 60)
    site.online[:, i0:i1] = False
    return Incident("site_outage", "site", site.site_id, "", "", _ts(site, i0), _ts(site, i1))


def inject_collection_gaps(cfg: GeneratorConfig, site: SiteArrays, rng: np.random.Generator,
                           busy: list[tuple[datetime, datetime]]) -> list[Gap]:
    """Drop whole polls for the site (collector down). Gaps avoid labeled outages so that
    'no data' and 'offline' never overlap in the ground truth."""
    gaps: list[Gap] = []
    for _ in range(cfg.collection_gaps_per_site):
        for _attempt in range(50):
            i0, i1 = _window(site, rng, 30, 3 * 60)
            g0, g1 = _ts(site, i0), _ts(site, i1)
            if all(g1 <= s or g0 >= e for s, e in busy):
                site.collected[i0:i1] = False
                gaps.append(Gap(site.site_id, g0, g1))
                break
    return gaps


def apply_offline_signal(site: SiteArrays) -> None:
    """Spec §3.1: signal is null when status = 0."""
    site.signal[~site.online] = np.nan
