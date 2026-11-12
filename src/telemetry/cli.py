"""Command line: ``telemetry generate | ingest | aggregate | check`` (spec §7).

Only ``generate`` exists so far (milestone M1).
"""
from __future__ import annotations

import argparse
import time
from datetime import date
from pathlib import Path

from .config import GeneratorConfig


def _generate(args: argparse.Namespace) -> None:
    from .generator import generate

    cfg = GeneratorConfig(
        seed=args.seed, sites=args.sites, ports_per_site=args.ports_per_site,
        devices_per_port=args.devices_per_port, poll_minutes=args.poll_minutes,
        start=date.fromisoformat(args.start), days=args.days, tz=args.tz, batch_hours=args.batch_hours,
    )
    t0 = time.perf_counter()
    res = generate(cfg, Path(args.out))
    print(f"{res.files:,} files · {res.rows:,} polls · {time.perf_counter() - t0:.1f}s -> {args.out}")
    print("incidents:    " + ", ".join(f"{k} {v}" for k, v in res.incidents.items()))
    print("data quality: " + ", ".join(f"{k} {v}" for k, v in res.data_quality.items()) + f", collection_gap {res.gaps}")


def main(argv: list[str] | None = None) -> None:
    d = GeneratorConfig()
    ap = argparse.ArgumentParser(prog="telemetry")
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate", help="write synthetic collector files and ground truth")
    g.add_argument("--seed", type=int, default=d.seed)
    g.add_argument("--sites", type=int, default=d.sites)
    g.add_argument("--ports-per-site", type=int, default=d.ports_per_site)
    g.add_argument("--devices-per-port", type=int, default=d.devices_per_port)
    g.add_argument("--poll-minutes", type=int, default=d.poll_minutes)
    g.add_argument("--start", default=d.start.isoformat(), help="local start date, YYYY-MM-DD")
    g.add_argument("--days", type=int, default=d.days)
    g.add_argument("--tz", default=d.tz)
    g.add_argument("--batch-hours", type=int, default=d.batch_hours)
    g.add_argument("--out", default="data")
    g.set_defaults(func=_generate)
    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
