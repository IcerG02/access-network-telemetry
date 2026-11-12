"""Synthetic topology: region -> site (OLT/CMTS) -> port/node -> device (spec §2, §4.1)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..config import GeneratorConfig

REGIONS = ("north", "south")


def build_topology(cfg: GeneratorConfig) -> pd.DataFrame:
    """One row per device. Sites alternate regions; the first half are GPON, the rest HFC."""
    n_gpon = cfg.sites // 2 + cfg.sites % 2
    rows = []
    for s in range(1, cfg.sites + 1):
        tech = "gpon" if s <= n_gpon else "hfc"
        site_id = f"s{s:02d}"
        region = REGIONS[(s - 1) % len(REGIONS)]
        for p in range(1, cfg.ports_per_site + 1):
            port_id = f"{site_id}-p{p:02d}"
            for d in range(1, cfg.devices_per_port + 1):
                rows.append((f"{tech}-{site_id}-p{p:02d}-d{d:03d}", port_id, site_id, region, tech))
    return pd.DataFrame(rows, columns=["device_id", "port_id", "site_id", "region", "tech"])


def base_signal(tech: str, n: int, rng: np.random.Generator) -> np.ndarray:
    """Per-device base signal level. Generator assumptions, not standards (spec §4.2)."""
    if tech == "gpon":
        return rng.uniform(-27.0, -8.0, n)   # ONT optical receive power, dBm
    return rng.uniform(35.0, 40.0, n)        # cable modem downstream SNR, dB
