"""Generator configuration (spec §4.1). Defaults reproduce the spec exactly."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class GeneratorConfig:
    seed: int = 42
    sites: int = 6                    # first half GPON, second half HFC
    ports_per_site: int = 8           # PON ports (GPON) or fiber nodes (HFC)
    devices_per_port: int = 32        # 1:32 splitter
    poll_minutes: int = 5             # 3 polls per 15-min window
    start: date = date(2026, 9, 21)   # local date; see spec §4.4 for why
    days: int = 21
    tz: str = "America/Sao_Paulo"     # calendar logic is local; files are UTC
    batch_hours: int = 6              # one collector file per site per batch
    # behaviour
    night_off_share: float = 0.12     # share of devices powered off on a given night (site average)
    # injected incidents (spec §4.3)
    device_drops_per_site: int = 12
    port_outages_per_site: int = 2
    site_outages: int = 2             # across the whole network
    degradation_share: float = 0.5    # share of port outages preceded by signal degradation
    # data-quality faults (spec §4.4)
    collection_gaps_per_site: int = 1

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    @property
    def start_utc(self) -> datetime:
        """Local midnight of ``start``, expressed in UTC."""
        local = datetime(self.start.year, self.start.month, self.start.day, tzinfo=self.zone)
        return local.astimezone(ZoneInfo("UTC"))

    @property
    def end_utc(self) -> datetime:
        local = datetime(self.start.year, self.start.month, self.start.day, tzinfo=self.zone) + timedelta(days=self.days)
        return local.astimezone(ZoneInfo("UTC"))

    @property
    def polls_per_device(self) -> int:
        return int((self.end_utc - self.start_utc).total_seconds() // 60 // self.poll_minutes)

    def validate(self) -> None:
        if self.sites < 2:
            raise ValueError("need at least 2 sites (one GPON, one HFC)")
        if (24 * 60) % self.poll_minutes or 15 % self.poll_minutes:
            raise ValueError("poll_minutes must divide 15")
        if 24 % self.batch_hours:
            raise ValueError("batch_hours must divide 24")
