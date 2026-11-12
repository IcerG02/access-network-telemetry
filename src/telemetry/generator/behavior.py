"""Normal behaviour of one site: daily power-off cycle and signal noise (spec §4.2).

Everything is a dense (devices x polls) array per site, so a full default run
(~9.3 million polls) stays vectorized and fast.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from ..config import GeneratorConfig
from .topology import base_signal


@dataclass
class SiteArrays:
    site_id: str
    tech: str
    devices: pd.DataFrame        # topology rows of this site
    poll_times: pd.DatetimeIndex # UTC, slot start of each poll
    online: np.ndarray           # bool  (devices, polls)
    signal: np.ndarray           # float (devices, polls); NaN where offline
    collected: np.ndarray        # bool  (polls,) False = collector gap, poll missing for the whole site


def poll_index(cfg: GeneratorConfig) -> pd.DatetimeIndex:
    return pd.date_range(cfg.start_utc, cfg.end_utc, freq=f"{cfg.poll_minutes}min", inclusive="left")


def simulate_site(cfg: GeneratorConfig, devices: pd.DataFrame, rng: np.random.Generator) -> SiteArrays:
    times = poll_index(cfg)
    n_dev, n_poll = len(devices), len(times)
    tech = devices["tech"].iloc[0]

    # Local clock, in hours since local midnight of the start date, for every poll.
    local = times.tz_convert(cfg.tz)
    hours_since_start = ((local - local[0]).total_seconds() / 3600).to_numpy()

    online = np.ones((n_dev, n_poll), dtype=bool)

    # Night cycle: each night a share of subscribers switches the device off,
    # roughly between 01:00 and 06:30 local time. The share varies per site.
    share = float(np.clip(rng.normal(cfg.night_off_share, cfg.night_off_share / 3), 0.02, 0.4))
    h = hours_since_start[None, :]
    for day in range(cfg.days):
        sleepers = rng.random(n_dev) < share
        off_at = day * 24 + rng.normal(1.0, 0.75, n_dev)              # ~01:00 local of `day`
        on_at = off_at + np.clip(rng.normal(5.5, 1.0, n_dev), 2.0, 9.0)  # back on ~06:30
        asleep = sleepers[:, None] & (h >= off_at[:, None]) & (h < on_at[:, None])
        online &= ~asleep

    # Background flakiness: a lost poll now and then.
    online &= rng.random((n_dev, n_poll)) > 0.002

    base = base_signal(tech, n_dev, rng)
    noise_sd = 0.3 if tech == "gpon" else 0.4
    signal = base[:, None] + rng.normal(0.0, noise_sd, (n_dev, n_poll))

    return SiteArrays(
        site_id=devices["site_id"].iloc[0], tech=tech, devices=devices.reset_index(drop=True),
        poll_times=times, online=online, signal=signal, collected=np.ones(n_poll, dtype=bool),
    )
