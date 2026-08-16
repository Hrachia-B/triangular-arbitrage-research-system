# Triangular Arbitrage Research System

A Python research system for testing spot triangular arbitrage with live public
order-book data and paper execution. It discovers cycles, maintains synchronized
local books, applies fees and exchange filters, walks displayed depth, rechecks
signals after latency intervals, and produces auditable reports.

## What this is

- A public-market-data observer for Binance Spot and MEXC Spot.
- A deterministic `Decimal`-based three-leg conversion simulator.
- A tool for measuring how fees, depth, rounding, latency, and conservative
  execution assumptions affect raw arbitrage signals.
- A research recorder and Markdown/CSV reporting pipeline.

## What this is not

- Not a live trading bot.
- Does not place or cancel orders.
- Does not read balances or manage inventory on an exchange.
- Does not prove live profitability.
- Does not contain API keys or authenticated trading endpoints.

## Research question

The system was built to test whether broad spot triangular-arbitrage signals
remain positive after:

1. taker fees on all three legs;
2. symbol filters and conservative rounding;
3. displayed multi-level order-book depth;
4. delayed reevaluation at 50-1,000 ms;
5. a pessimistic quantity haircut and adverse-price buffer.

Raw top-of-book edge is recorded, but it is not treated as executable profit.

## Exchanges tested

- Binance Spot through public market-data-only REST and WebSocket hosts.
- MEXC Spot through public REST and protobuf WebSocket market data.

MEXC has an optional read-only fee checker. Normal observation requires no
credentials.

## Main result

The tested broad configurations did not produce a deployable strategy.
Binance raw signals were eliminated by the configured fees. MEXC produced rare
positive observations, but its completed 48-hour run remained negative overall,
with a 99.71% ghost rate and only four pessimistic survivors.

| Exchange | Duration | Cycles | Fee source or assumption | Pessimistic survivors | Total simulated PnL | Research decision |
|---|---:|---:|---|---:|---:|---|
| Binance Spot | 60 min | 50 | 0.10% per leg | 0 / 152,920 | not retained | STOP |
| MEXC Spot | 48 h | 10 | sanitized schedule; max/fallback 0.05% | 4 / 7,329 | -474.055349 | Do not trade |

The Binance row is reconstructed from metrics retained in the project
documentation because its original generated report is not available in this
checkout. The MEXC row comes from the sanitized final artifacts in
[`public_results/mexc`](public_results/mexc).

## How it works

```text
public exchange metadata and tickers
                 |
                 v
       symbol and cycle discovery
                 |
                 v
 public depth streams + REST snapshots
                 |
                 v
 synchronized, health-checked local books
                 |
                 v
 raw screen -> fees -> depth -> pessimistic model
                 |
                 v
          latency rechecks
                 |
                 v
      JSONL records + Markdown/CSV reports
```

Cycle conversions use bids for base sales and asks for base purchases. Fees are
deducted from each leg's output. The depth pass consumes displayed levels and
applies quantity, notional, price, and lot constraints. The pessimistic pass
reduces displayed quantity and shocks later-leg prices.

See [`docs/methodology.md`](docs/methodology.md) for formulas and decision gates.

## Installation

Python 3.12 is recommended; Python 3.11 is supported.

Windows:

```powershell
git clone https://github.com/<owner>/triangular-arbitrage-research-system.git
cd triangular-arbitrage-research-system
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pytest
```

macOS/Linux:

```bash
git clone https://github.com/<owner>/triangular-arbitrage-research-system.git
cd triangular-arbitrage-research-system
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pytest
```

Recorded development versions are listed in
[`docs/environment_versions.md`](docs/environment_versions.md).

## Running a safe simulation

Binance, using the checked-in profile:

```powershell
python -m tri_arb.main --exchange binance --duration-minutes 10 --max-cycles 20 `
  --storage-mode compact --min-free-gib 5
```

MEXC, using an explicit simulated fee:

```powershell
python -m tri_arb.main --exchange mexc --duration-minutes 10 --max-cycles 20 `
  --fee-rate 0.0005 --storage-mode compact --min-free-gib 5
```

Use `--data-dir` to place generated data outside the checkout and
`--storage-mode compact` for bounded long-run persistence:

```powershell
python -m tri_arb.main --exchange mexc --duration-minutes 10 --max-cycles 20 `
  --fee-rate 0.0005 --data-dir "E:\tri-arb-data" --storage-mode compact `
  --min-free-gib 5
```

Every command above uses public market data and paper simulation. Run
`python -m tri_arb.main --help` for all options.

## Optional MEXC fee check

The fee checker is isolated from the public observer and permits one
authenticated request: signed read-only `GET /api/v3/tradeFee`. It cannot query
balances or place orders.

Copy the placeholder file and edit it locally:

```powershell
Copy-Item .env.example .env
notepad .env
```

Use a dedicated MEXC key with trading, transfers, and withdrawals disabled.
Never commit `.env` or paste its contents into an issue.

Run public discovery, then perform a bounded fee check:

```powershell
python -m tri_arb.main --exchange mexc --discover-only `
  --storage-mode compact --min-free-gib 1
$selection = Get-ChildItem .\data\raw\selected_symbols_*.json |
  Sort-Object LastWriteTimeUtc -Descending |
  Select-Object -First 1 -ExpandProperty FullName
python -m tri_arb.tools.check_mexc_fees --discovery-selection $selection
```

The normalized fee schedule is local and ignored by Git. Published results use
sanitized numeric assumptions rather than authenticated response data.

## Reports

Default output is written below `data/`:

```text
data/
  raw/          discovery and raw observations
  signals/      fee, depth, pessimistic, and latency records
  snapshots/    optional book snapshots
  logs/         operational logs
  reports/      Markdown and CSV reports
```

`data/reports/latest.md` and `latest_summary.csv` point to the most recent
completed run. Timestamped reports and checkpoints retain run history.

The committed [`public_results`](public_results) package contains sanitized,
compact experiment outputs. Raw exchange streams, logs, credentials, and
authenticated responses are intentionally absent.

Compare local reports with:

```powershell
$reports = Get-ChildItem .\data\reports\report_*.md | Select-Object -ExpandProperty FullName
python -m tri_arb.tools.compare_reports $reports
```

## Reproducing the published analysis

The aggregate public files support review of counts, edges, PnL, latency
survival, cycle concentration, and data quality. The raw streams are not
published, so exact event-by-event replay is not available. A new live-data run
samples a different market regime and will not reproduce identical metrics.

See:

- [`docs/experiments.md`](docs/experiments.md)
- [`docs/reproducibility.md`](docs/reproducibility.md)
- [`public_results/README.md`](public_results/README.md)

## Safety boundaries

- Public observers use allow-listed market-data endpoints.
- The optional MEXC authenticated client allows only `GET /api/v3/tradeFee`.
- No order, cancel, balance, transfer, withdrawal, or execution method exists.
- Credentials are read locally, redacted from errors, and excluded from reports.
- Tests enforce the endpoint allow-list and absence of order operations.

Details and the publication checklist are in
[`docs/security.md`](docs/security.md).

## Repository structure

```text
configs/          checked-in Binance and MEXC research profiles
docs/             methodology, experiments, security, and runbooks
public_results/   sanitized aggregate experiment artifacts
scripts/          Windows setup and guarded research-run helpers
tests/            offline unit and integration tests with synthetic data
tri_arb/          discovery, order books, simulation, recording, and reports
pyproject.toml    package metadata and tool configuration
requirements.txt compatible runtime and development dependency ranges
```

## Limitations

- Paper fills are not real fills.
- Displayed liquidity can disappear or be consumed ahead of an order.
- Queue position and matching-engine latency are unknown.
- Public WebSocket timing differs from authenticated order latency.
- Three real legs would not execute atomically and would carry inventory risk.
- Compact mode retains exact cumulative aggregates and bounded samples/top
  records, not every raw event.
- Fee schedules, exchange filters, and market regimes change.
- The published aggregates cannot independently verify every source event.
- No result in this repository is a profitability claim or trading advice.

## Conclusion

The project turns a market-microstructure hypothesis into measurable evidence.
Under the tested conditions, broad spot triangular arbitrage on Binance and
MEXC was not strong enough to justify live execution. The code remains useful
for public exchange-data research, controlled replay work, and testing other
paper-execution assumptions while preserving a strict no-trading boundary.
