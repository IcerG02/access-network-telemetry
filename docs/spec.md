# access-network-telemetry — Specification

> Status: **draft for review** · Author: Eric Guilherme · Written with Claude (AI-assisted, spec-first)
>
> This document is the contract for the project. Code is written against it, and any change in
> behavior starts as a change here. Sections marked **DECISION** are open and need an answer
> before the milestone that depends on them.

---

## 1. Purpose

Build, from scratch and with **synthetic data only**, an end-to-end pipeline that turns raw device
polls from a fixed-access telecom network (GPON fiber and HFC cable) into reliable availability
aggregates — and prove it is reliable with an automated validation suite checked against
ground truth.

The project exists to show, in public, the data-engineering judgment that production telemetry
pipelines demand: idempotent ingestion, duplicate files, late data, missing polls, timezone and
calendar boundaries, and aggregates that still add up at the end of the month.

### Non-goals (v1)

- No real network data, no real company, site or table names. Everything is generated.
- No dashboards or alerting UI. Output is tables plus a validation report.
- No streaming. Batch processing on files, like a collector that drops CSVs into a folder.
- No anomaly detection or forecasting yet. The data is designed so that a later version can add it.

---

## 2. Domain in one page

| Term | GPON (fiber) | HFC (cable) |
|---|---|---|
| Head-end equipment | **OLT** (optical line terminal) | **CMTS** (cable modem termination system) |
| Aggregation point | **PON port** on the OLT, shared through splitters | **Fiber node**, serving a neighborhood over coax |
| Subscriber device | **ONT/ONU** | **Cable modem** |
| Signal metric used here | ONT optical receive power, **dBm** | Downstream SNR, **dB** |

A **collector** polls every subscriber device on a fixed interval and records whether it answered
(online) or not (offline), plus one signal reading. Operations teams care about **how many devices
are online, per site, over time** — and about outages that take down a whole port or node at once.

Topology modeled (same shape for both technologies):

```
region ─► site (OLT / CMTS) ─► port / node ─► device (ONT / cable modem)
```

---

## 3. Data contracts

### 3.1 Collector files (input)

One CSV per site per collection batch, dropped into `data/inbox/`.

- File name: `{tech}_{site_id}_{YYYYMMDDHHMM}.csv` — `tech` ∈ `gpon`, `hfc`; timestamp = batch start, UTC.
- Columns:

| Column | Type | Notes |
|---|---|---|
| `device_id` | text | Stable across the run, e.g. `gpon-s03-p07-d041` |
| `polled_at` | timestamp, UTC, ISO 8601 | Time of the poll |
| `status` | int, `1` / `0` | 1 = answered, 0 = did not answer |
| `signal` | float, nullable | dBm (GPON) or dB (HFC). Null when `status = 0` |

### 3.2 Database (PostgreSQL)

| Table | Grain | Purpose |
|---|---|---|
| `topology` | device | device → port/node → site → region, technology |
| `ingested_files` | file | MD5, file name, row count, loaded_at, outcome — the duplicate-file registry |
| `polls` | device × poll | Raw readings. Unique key `(device_id, polled_at)` |
| `overflow` | device × poll | Rows that arrived in a run for a different reference month (see §5.4) |
| `status_15m` | site × 15-min window | Devices total / online, signal avg, number of polls seen |
| `agg_hourly` | site × hour | Derived from `status_15m` |
| `agg_daily` | site × day | Derived from `agg_hourly` |
| `agg_weekly` | site × ISO week | Derived from `agg_daily` — see the month ownership rule |
| `agg_monthly` | site × month | Derived from `agg_daily` (not from weekly) |
| `run_log` | run | What each pipeline run did, for audit |

All timestamps are stored as `timestamptz` (UTC). **Calendar logic — days, weeks and months —
runs in local time**, configurable, default `America/Sao_Paulo`. (Brazil has had no daylight
saving time since 2019, but the code must not assume that: the timezone is a parameter.)

---

## 4. Synthetic generator

Command: `telemetry generate`. Deterministic for a given `--seed`.

### 4.1 Topology

| Parameter | Default | Notes |
|---|---|---|
| `--sites` | 6 (3 GPON, 3 HFC) | |
| `--ports-per-site` | 8 | PON ports or fiber nodes |
| `--devices-per-port` | 32 | Matches a common 1:32 splitter ratio |
| `--poll-minutes` | 5 | 3 polls per 15-min window |
| `--start` / `--days` | `2026-09-21`, 21 days | Chosen on purpose, see §4.4 |

Default volume: 6 × 8 × 32 = **1,536 devices** × 288 polls/day × 21 days ≈ **9.3 million rows**.
Large enough to make performance real; small enough for a laptop.

### 4.2 Normal behavior

- **Daily cycle:** a share of subscribers power their device off at night, so the online count dips
  overnight and recovers in the morning. Amplitude configurable per site.
- **Signal:** each device has a base level drawn once (GPON roughly −8 to −27 dBm; HFC roughly
  35 to 40 dB SNR — generator assumptions, not standards) plus small noise per poll.

### 4.3 Injected faults — with labels

Every fault is written to `data/ground_truth/incidents.csv` (`kind`, `scope`, `site_id`,
`port_id`, `device_id`, `start`, `end`), so the pipeline can be validated against what actually
happened.

| Kind | Scope | Effect |
|---|---|---|
| `device_drop` | one device | Offline for a random period |
| `port_outage` | port / node | **Every device behind it** goes offline at once — the cascade pattern |
| `site_outage` | site | Whole site offline (e.g. power) |
| `degradation` | port / node | Signal drifts down for hours **before** a port outage. Groundwork for anomaly detection later |

### 4.4 Data-quality faults — the ones that break naive pipelines

| Fault | What the generator does | What the pipeline must do |
|---|---|---|
| **Missing polls** | Drops whole polls for a site (collector gap) | Window = *no data*, never "all offline" |
| **Duplicate file** | Same file delivered twice | Load once |
| **Renamed duplicate** | Same content, different file name | Load once — detect by content (MD5), not by name |
| **Late file** | A batch for 30 Sep delivered among October files | Route to the right month (§5.4) |
| **Out-of-order files** | Files arrive shuffled | Result independent of arrival order |
| **Malformed file** | Missing column / bad value | Reject to `data/failed/` with reason; never half-load |

The default period, 21 Sep – 11 Oct 2026, is chosen to force calendar edge cases:
the ISO week **Mon 28 Sep – Sun 4 Oct crosses the month boundary**, and late files cross it too.

---

## 5. Pipeline

### 5.1 Ingest — `telemetry ingest`

1. Scan `data/inbox/` (sorted, but correctness must not depend on the order).
2. For each file: compute MD5. If the MD5 is already in `ingested_files` → skip, log `duplicate`.
3. Validate schema and values **before** loading. On failure → move to `data/failed/` with a reason file.
4. Load with `COPY` into a staging table, then insert into `polls` with `ON CONFLICT DO NOTHING`
   (second line of defense for overlapping files).
5. Record the file in `ingested_files`; move it to `data/processed/`.

The whole step is **idempotent**: running it twice changes nothing the second time.

### 5.2 15-minute consolidation

Per site and 15-minute window (local time): `devices_total`, `devices_online`, `signal_avg`,
`polls_seen`.

> **DECISION 1 — online count within a window.** With 3 polls per window, which number is
> "online"?
> - **A. Per device:** a device counts as online if it answered at least one poll in the window.
>   Robust to a single lost poll; slightly optimistic.
> - **B. Running max of the site count:** count online devices at each poll, take the max of the
>   three. Cheaper, and it never counts the same device twice.
>
> Status: open. The chosen option and its reasoning will be recorded in `docs/decisions/`.

A window with **zero polls** is stored with `devices_online = NULL` and `polls_seen = 0`.
A gap in collection is not an outage.

### 5.3 Rollups

- **Hourly:** from `status_15m`.
- **Daily:** from hourly with **native `AVG`, never `SUM`**. `AVG` skips the `NULL` windows from
  collection gaps, so a site with 21 hours of data is averaged over 21, not divided by 24.
- **Weekly:** ISO weeks, Monday to Sunday (`EXTRACT(ISODOW ...)`).
  **A week belongs to the month of its Monday.** The week of Mon 28 Sep – Sun 4 Oct belongs to
  September and appears only in September's weekly results. Without this rule the same week is
  counted in two months.
- **Monthly:** from **daily**, by calendar month — not from weekly, which would reintroduce the
  boundary problem.

Metrics per aggregate: `avg_online`, `min_online` (worst window), `availability_pct`
(`avg_online / devices_total`), `windows_with_data` (completeness), `signal_avg`.

### 5.4 Reference month and overflow — `telemetry aggregate --month 2026-09`

Each aggregation run has a **reference month**. Rows that belong to another month are not
dropped and not silently mixed in:

- Rows for a **future or not-yet-processed** month go to `overflow` and are loaded automatically
  when that month's run happens.
- Rows for a month **already closed** go to `overflow` and mark that month **dirty**. The next run
  recomputes every dirty month and reports it in `run_log`.

---

## 6. Validation suite — `telemetry check --month 2026-09`

The pipeline is only as good as the evidence that it is right. Each check prints PASS/FAIL with
numbers and exits non-zero on any failure, so it runs in CI.

| Check | Rule |
|---|---|
| `completeness` | Every site has 96 windows per local day; gaps are only where the generator dropped polls |
| `reconcile` | `devices_online ≤ devices_total` everywhere; totals match `topology` |
| `no_duplicates` | No duplicate `(device_id, polled_at)`; each MD5 loaded at most once |
| `week_ownership` | No ISO week appears in more than one month; the cross-month week is in September |
| `recompute` | For a random sample of days, rollups recomputed from raw `polls` match the stored ones |
| `order_independence` | Ingesting the same files in a shuffled order produces identical aggregates |
| `incidents_visible` | Every `port_outage` and `site_outage` in ground truth shows up as a drop in `status_15m` for the right site and time, and no unexplained drop of the same size exists |

---

## 7. Stack

Python 3.11+, pandas, SQLAlchemy, psycopg2, PostgreSQL 16, pytest, GitHub Actions.
CLI: `telemetry generate | ingest | aggregate | check`.

> **DECISION 2 — how PostgreSQL runs:** Docker Compose (anyone can clone and run it, CI uses the
> same thing) or a local install. Recommendation: Docker Compose.

---

## 8. Repository layout

```
access-network-telemetry/
├── README.md               # story, diagram, how to run, results in numbers
├── LICENSE                 # MIT
├── pyproject.toml
├── docker-compose.yml      # PostgreSQL 16
├── Makefile                # make demo = generate → ingest → aggregate → check
├── docs/
│   ├── spec.md             # this file
│   └── decisions/          # one short note per DECISION, with the reasoning
├── sql/
│   ├── schema.sql
│   └── rollups/            # 15m, hourly, daily, weekly, monthly
├── src/telemetry/
│   ├── cli.py
│   ├── config.py
│   ├── generator/          # topology, behavior, faults, writer
│   ├── ingest/             # scan, validate, dedup, load
│   ├── aggregate/          # runs the SQL, handles overflow and dirty months
│   └── checks/
├── tests/
└── data/                   # git-ignored: inbox, processed, failed, ground_truth
```

---

## 9. Milestones

| # | Delivers | Done when | Written by |
|---|---|---|---|
| M1 | Generator + ground truth | Same seed → byte-identical files; every fault kind present | Claude drafts, Eric reviews |
| M2 | Schema + ingest | Duplicate, renamed and malformed files handled; running twice changes nothing | Eric: dedup and validation rules · Claude: plumbing |
| M3 | 15-min, hourly, daily | DECISION 1 implemented; `completeness` and `reconcile` pass | **Eric: rollup SQL** |
| M4 | Weekly, monthly, overflow | `week_ownership` passes; late file lands in September | **Eric: week ownership and overflow** |
| M5 | Full check suite + CI | `make demo` green locally and in GitHub Actions | Claude drafts, Eric reviews |
| M6 | README | Architecture diagram, how to run, results table, "How this was built" | Together |

"Written by" is deliberate: the core data rules are written by hand; scaffolding and plumbing
are AI-assisted. The README says so plainly.

---

## 10. Open questions

1. **DECISION 1** — online count within a 15-min window (§5.2).
2. **DECISION 2** — PostgreSQL via Docker Compose or local install (§7).
3. Scale defaults (§4.1): keep ~9 million rows, or go smaller for faster iteration while
   developing and larger for a final benchmark in the README?
