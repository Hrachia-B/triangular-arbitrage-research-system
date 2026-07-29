# Binance and MEXC Spot Triangular-Arbitrage Research Simulator

This project observes public Binance or MEXC Spot order books, discovers root-anchored
triangular routes, and paper-simulates their execution through real displayed
depth. It is intended to answer a narrow research question: does an apparent
three-leg price discrepancy survive taker fees, lot/notional constraints,
visible liquidity, slippage assumptions, and realistic observation latency?

> **Research only — never a trading bot.** The simulator and both exchange
> adapters use public market data only and contain no order placement,
> cancellation, balance, wallet, transfer, or private-user-stream logic. One
> isolated utility can sign the read-only MEXC `GET /api/v3/tradeFee` request
> to learn account-specific fee rates. Those credentials are never available
> to the simulator.

This system does **not** prove live profitability. It only estimates whether
opportunities might be realistic enough to justify deeper testing.

## What it does

- Loads current Spot symbol metadata and exchange filters from
  `GET /api/v3/exchangeInfo`.
- Ranks a configurable symbol universe using rolling 24-hour public statistics.
- Discovers both directions of `ROOT -> A -> B -> ROOT` routes instead of
  hardcoding a triangle.
- Opens public exchange-specific depth streams before taking REST depth snapshots.
- Maintains Decimal-based local books using each exchange's documented sequence
  rules, excludes unhealthy books, and resynchronizes after gaps. MEXC aggregate
  depth is decoded from its official protobuf schema rather than treated as JSON.
- Evaluates top-of-book, depth-aware, latency-rechecked, and pessimistic fills.
- Keeps the raw no-fee price discrepancy separate from executable filter checks,
  so minimum-notional and lot-size ghosts remain visible in the data.
- Applies a fee on each leg, walks actual bid/ask quantities, checks common
  `LOT_SIZE`, `MARKET_LOT_SIZE`, `MIN_NOTIONAL`, and `NOTIONAL` constraints, and
  records partial-depth failures.
- Persists structured JSONL observations and produces Markdown and CSV reports
  at shutdown.
- Publishes hourly checkpoint reports during long runs and applies a
  configurable 48-hour `STOP` / `CONTINUE_TO_7D` / `UNCLEAR` research
  decision.
- Optionally applies symbol-specific taker fees obtained by the isolated,
  read-only MEXC fee checker.

It does **not** place, test-place, prepare, or submit orders. It does not inspect
balances. Only `tri_arb.tools.check_mexc_fees` accepts API credentials, and its
client is hard-limited to one authenticated read-only endpoint.

## Installation

Python 3.11 or newer is recommended.

```bash
cd triangular_arbitrage_system
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

The runtime dependencies are deliberately small: `aiohttp`, `websockets`,
`protobuf`, and `PyYAML`. `pytest`, `pytest-asyncio`, and `ruff` are included for
verification.

For a Windows transfer, use the checked-in PowerShell setup instead of copying
the Mac virtual environment. See the
[Windows runbook](docs/windows_runbook.md) and
[recorded environment versions](docs/environment_versions.md).

## Running

Run these commands from the repository root after activating `.venv`. Each
command names the exchange explicitly; the full-profile commands also name the
checked-in YAML so copied runbooks retain every assumption.

Binance discover-only (public REST metadata and ticker statistics; no depth streams):

```bash
python -m tri_arb.main --exchange binance --config configs/default.yaml --discover-only
```

Ten-minute test:

```bash
python -m tri_arb.main --exchange binance --config configs/default.yaml --duration-minutes 10
```

One-hour test:

```bash
python -m tri_arb.main --exchange binance --config configs/default.yaml --duration-minutes 60
```

Report-only mode for the most recently completed run (no network access):

```bash
python -m tri_arb.main --exchange binance --config configs/default.yaml \
  --report-only "$(ls -t data/reports/summary_metrics_*.jsonl | head -n 1)"
```

Binance remains the CLI default, so older commands without `--exchange` still
work. The explicit form above makes the selected adapter visible in shell
history and in copied runbooks.

### MEXC commands

Run MEXC discovery first. It uses only public REST data and opens no depth
streams:

```bash
python -m tri_arb.main --exchange mexc --discover-only
```

Ten-minute MEXC smoke test:

```bash
python -m tri_arb.main --exchange mexc --duration-minutes 10 --max-cycles 20
```

One-hour MEXC simulation:

```bash
python -m tri_arb.main --exchange mexc --duration-minutes 60 --max-cycles 50
```

Run the complete checked-in MEXC research profile:

```bash
python -m tri_arb.main --exchange mexc --config configs/mexc_default.yaml
```

Generate an offline report from a MEXC run without any network connection:

```bash
python -m tri_arb.main --exchange mexc --config configs/mexc_default.yaml \
  --report-only "$(ls -t data/reports/summary_metrics_*.jsonl | head -n 1)"
```

Press `Ctrl-C` for a graceful shutdown; the recorder is closed and a final
report is still generated.

### Read-only MEXC account-fee check

MEXC fee campaigns, regions, account tiers, and symbols can differ. To check
the fee returned for the actual account, create a dedicated API key with
trading, transfers, and withdrawals disabled. Add it to a local `.env` copied
from `.env.example`:

```bash
cp .env.example .env
chmod 600 .env
```

Set `MEXC_API_KEY` and `MEXC_API_SECRET` in that file, then run:

```bash
python -m tri_arb.tools.check_mexc_fees
```

With no symbol option, the checker requests the available fee schedule in one
call. It can also check explicit symbols or reuse a discovery selection:

```bash
python -m tri_arb.tools.check_mexc_fees --symbols BTCUSDT,ETHUSDT
python -m tri_arb.tools.check_mexc_fees \
  --discovery-selection data/raw/selected_symbols_<run-id>.json
```

MEXC's current Spot V3 documentation marks `symbol` as mandatory for this
endpoint, even though the requested default mode attempts one no-symbol
all-fees call. If MEXC rejects that call, use `--symbols` or first run
discover-only and pass its `selected_symbols_<run-id>.json`. Explicit requests
are sequential, paced, and use bounded rate-limit retries; the checker never
fans them out concurrently.

The checker signs only `GET /api/v3/tradeFee`. It cannot call order, cancel,
balance, transfer, or withdrawal endpoints. It never prints or persists the
secret. Outputs are local and ignored by Git:

```text
data/account/mexc_fees_<timestamp>.json
configs/generated/mexc_account_fee.yaml
```

The normalized file contains per-symbol fees and the maximum observed taker
fee as a conservative fallback. It contains no API key or secret.

### Choosing the simulation fee

The three fee modes are deliberately explicit:

- the YAML profile uses `config_fee`;
- `--fee-rate` uses `fixed_cli_fee`;
- `--use-account-fees` uses `mexc_account_tradeFee_read_only`.

Run a 10-minute MEXC validation using the generated account fee schedule:

```bash
python -m tri_arb.main --exchange mexc \
  --duration-minutes 10 --max-cycles 20 --use-account-fees
```

If the generated fee file is absent, account-fee mode stops with:

```text
Run python -m tri_arb.tools.check_mexc_fees first.
```

Each leg uses its symbol-specific taker fee when available. A missing symbol
uses the maximum taker fee in the generated schedule. The selected fees affect
top-of-book scanning, depth simulation, the pessimistic model, latency
rechecks, and the final report.

For controlled sensitivity experiments, use a fixed fee override instead:

```bash
python -m tri_arb.main --exchange mexc --duration-minutes 60 --max-cycles 20 --fee-rate 0.0002
python -m tri_arb.main --exchange mexc --duration-minutes 60 --max-cycles 20 --fee-rate 0.0001
python -m tri_arb.main --exchange mexc --duration-minutes 60 --max-cycles 20 --fee-rate 0.0
```

A zero-fee override is a scenario, not evidence that the account or selected
symbols actually have zero fees. Do not combine a fixed override with
`--use-account-fees`.

### 48-hour and 7-day research runs

After the fee check and a successful 10-minute validation, the 48-hour command
is:

```bash
python -m tri_arb.main --exchange mexc \
  --use-account-fees --duration-minutes 2880 --max-cycles 20
```

The default checkpoint interval is 60 minutes. It can be changed for one run
with `--checkpoint-every-minutes`; `0` disables checkpoints. During a long run,
inspect:

```text
data/reports/latest.md
data/reports/latest_summary.csv
data/reports/checkpoint_<timestamp>.md
```

The report's `48H_DECISION` is:

- `STOP` when a meaningful completed sample has no depth or pessimistic
  survivors, more than the configured ghost threshold, and no positive best
  net edge;
- `CONTINUE_TO_7D` only when realistic survivors repeat by cycle, survive a
  configured 50 ms or 100 ms check, and have non-negative aggregate diagnostic
  PnL, with positive evidence persisted across at least two hourly
  checkpoints;
- `UNCLEAR` when all conditions for neither decision are satisfied.

Thresholds live in the checked-in `decision` configuration section. Only
`CONTINUE_TO_7D` justifies collecting a longer sample. It is not authorization
to trade.

If that exact decision is present, the 7-day command is:

```bash
python -m tri_arb.main --exchange mexc \
  --use-account-fees --duration-minutes 10080 --max-cycles 20
```

On Windows, use the guarded scripts instead:

```powershell
.\scripts\setup_windows.ps1
.\scripts\check_mexc_fees.ps1
.\scripts\run_mexc_10min.ps1
.\scripts\run_mexc_48h.ps1
# Only after 48H_DECISION: CONTINUE_TO_7D
.\scripts\run_mexc_7d.ps1
```

The long-run scripts keep console copies under `data\logs\`, require the local
fee configuration, and refuse to start below their disk-space safety floors.
Follow all power, sleep, storage, stopping, and report-export instructions in
the [Windows runbook](docs/windows_runbook.md).

## Research status: Binance STOP, MEXC next

The latest 60-minute Binance run monitored 39 symbols and 50 directional
cycles. It recorded 152,920 raw opportunities, but none remained profitable
after the configured taker fees or displayed-depth simulation; latency analysis
classified 100% of the sampled signals as ghost arbitrage. Its research
conclusion was **STOP**: the observed raw edges did not cover the assumed fees.

MEXC is the next public-data candidate because its much larger Spot universe
may produce different triangle coverage and because advertised fee campaigns
can materially change the break-even threshold. Those same properties demand
stricter liquidity, spread, top-of-book-notional, asset, and symbol-pattern
filters. The MEXC profile therefore treats several fee rates as scenarios; it
does not assume that an advertised zero-fee rate applies to a particular user,
region, symbol, or time.

> **MEXC fee warning:** Even the read-only `tradeFee` response is an input to a
> simulation, not a fill guarantee. Verify the applicable fee on the actual
> MEXC account page and against trade history before drawing any execution
> conclusion.

## Configuration

[`configs/default.yaml`](configs/default.yaml) is the Binance profile and
[`configs/mexc_default.yaml`](configs/mexc_default.yaml) is the stricter MEXC
profile. Important assumptions include:

- root asset and important bridge assets;
- maximum selected symbols/cycles and WebSocket streams per connection;
- root-denominated quote-volume, spread, and top-of-book-notional filters;
- excluded assets and symbol-name glob patterns;
- REST snapshot depth, locally retained levels, and book staleness limit;
- taker fee, root-asset starting sizes, scan cadence, and signal cooldown;
- account-fee fallback behavior, checkpoint cadence, and 48-hour decision
  thresholds;
- latency recheck buckets;
- displayed-quantity haircut and extra adverse slippage for the pessimistic run;
- output directory, rotation size, and log level.

The defaults use `https://data-api.binance.vision` and
`wss://data-stream.binance.vision:443`, Binance's public market-data-only
services. They cannot serve private account or order traffic. The regular
public Binance hosts can be configured where the market-data-only domains are
unavailable, but this package still only permits the three public GET paths it
needs.

Fees are modeled as a taker charge deducted from each leg's output asset. The
source can be a checked-in config value, an explicit CLI override, or the local
read-only MEXC fee schedule. Actual charged fees, discounts, campaigns, tiers,
and fee rounding can still differ; reports preserve the source and effective
assumptions instead of presenting them as live execution evidence.

The checked-in 30–60 minute profile selects up to 50 symbols and 50 directional
cycles (25 complete forward/reverse pairs). Discovery can return fewer when the
eligible market does not contain enough complete triangles, so use the
discover-only command to confirm the current count before a long run. It keeps
100 local depth levels, uses one combined connection for at most 50 public
`@depth@100ms` streams, scans every 50 ms, and suppresses duplicate signals for
the full 1000 ms latency horizon.

At startup, 50 snapshots with `limit=100` consume 250 request-weight units in
the normal case. Together with one all-symbol 24-hour ticker request (80) and
one exchange-information request (20), the expected startup total is 350. This
is intentionally bounded, and the client backs off on rate-limit responses.
Binance currently documents a weight of 5 for depth limits up to 100 and a
maximum of 1024 streams per WebSocket connection; the profile stays well below
both practical ceilings.

## Runtime design

```text
exchangeInfo + 24h tickers
           |
           v
symbol filter/ranking -> triangle construction/ranking
                                   |
                                   v
                    public diff streams (opened first)
                                   + REST depth snapshots
                                   |
                                   v
                    synchronized healthy local books
                                   |
                                   v
           top estimate -> depth walk -> latency checks
                                   + pessimistic replay
                                   |
                                   v
                     JSONL recorder -> Markdown/CSV report
```

The core money and quantity path uses `Decimal`. A sell-base leg consumes bids;
a buy-base leg spends quote currency against asks. Fees are applied after every
conversion, so the fee-reduced output of one leg is the input to the next. Lot
rounding can leave several small asset balances: results expose actual converted
root cash separately and conservatively mark residual inventory through the
remaining route for PnL, instead of treating retained dust as lost capital.

The local book follows the selected exchange's required ordering: buffer depth
messages, fetch a snapshot, discard updates already covered by the snapshot
version, require the first retained event to bridge that snapshot, and
resynchronize after a sequence gap. For MEXC, every later `fromVersion` must be
the previous `toVersion + 1`; pushed quantities are absolute and zero removes a
level. Exchange event time and local receive and update times are kept separately.

## Output

Each run writes under `data/` (or the configured output directory). From the
repository root, Markdown reports are therefore in `data/reports/`:

```text
data/
  raw/        # discovery selections, raw opportunities, and book health
  signals/    # canonical signals plus fee/depth/pessimistic/latency stages
  reports/    # Markdown report, scalar summary, and top-opportunity CSV
  snapshots/  # reserved for optional book-snapshot captures
  logs/       # rotating operational logs and diagnostic errors
```

The Markdown report includes counts at each realism stage, edge and estimated
PnL distributions, raw/net edge percentiles, break-even-fee and fee-sensitivity
analysis, the top 20 raw opportunities, per-cycle diagnostics, target versus
actual latency (including scheduler lateness), latency coverage, ghost-signal
share, book staleness and resynchronization health, assumptions, and one
conclusion. The same run also writes `top_opportunities_<timestamp>.csv` for
machine-readable inspection.

Long runs additionally update `latest.md` and `latest_summary.csv` atomically
and retain timestamped checkpoint Markdown files. Generated reports remain on
disk but, like raw observations and account fee responses, are ignored by Git.

- `PROCEED`: conservative observations justify more research, not live trading.
- `UNCLEAR`: the sample or infrastructure is insufficient for a decision.
- `STOP`: no persistent conservative edge was observed.

A positive raw count with no depth-aware survivors is normal: crossed timing,
fees, thin levels, filter rounding, and propagation delay routinely turn a
mathematical loop into a non-executable one. Treat `PROCEED` as permission to
collect better data, never as authorization to trade.

### Comparing reports

Compare the allow-listed research metrics from multiple Markdown reports:

```bash
python -m tri_arb.tools.compare_reports data/reports/*.md
```

The table includes exchange, fee assumption and source, duration, monitored
symbols and cycles, fee/depth/pessimistic survivors, realistic edge and PnL,
ghost percentage, best cycles, conclusion, and `48H_DECISION`. It does not
read raw market-data or account-response files.

In PowerShell, expand the files before invoking Python:

```powershell
$reports = Get-ChildItem .\data\reports\*.md | Select-Object -ExpandProperty FullName
& .\.venv\Scripts\python.exe -m tri_arb.tools.compare_reports $reports
```

### Storage warning

Raw and per-stage JSONL output is intentionally detailed and can be very large.
A previous 20-cycle sample produced about 1.6 GiB in roughly 54 minutes, which
can extrapolate to around 85–90 GiB over 48 hours and roughly 500 GiB over 7
days. Market activity can make the actual total larger. Check free space
throughout the run; checkpoints do not replace or truncate the raw evidence.

## Verification

```bash
pytest
ruff format .
ruff check .
```

The tests use synthetic symbol metadata and hand-checked books; they do not
depend on Binance, MEXC, or network availability. They cover route direction, fees on
all legs, multi-level depth consumption, insufficient depth, exchange filters,
snapshot/diff sequencing and gaps, protobuf decoding, discovery/ranking,
latency outcomes, and report generation.

They also enforce the authenticated fee client's single-endpoint allow-list,
credential redaction, account-fee fallback behavior, and the absence of order
endpoints.

## Safe private GitHub transfer

The repository must remain private. `.gitignore` excludes `.env`, virtual
environments, account fee responses, generated fee configs, raw observations,
signals, snapshots, logs, and generated reports—including nested
exchange-specific data directories. Existing local reports are not deleted;
they are simply excluded from commits.

Before the first commit, inspect exactly what Git will include:

```bash
git init
git status --ignored
git add .
git diff --cached
git ls-files
```

Search staged/source content for credential-shaped text and inspect every
match. Placeholder names in `.env.example` and safety tests are expected; real
values are not:

```bash
git grep --cached -n -I -E 'MEXC_API_KEY|MEXC_API_SECRET|api_key|secret|private|token'
```

If `gitleaks` is installed, also run `gitleaks detect --source .` and resolve
every finding before committing.

If GitHub CLI is installed and authenticated, create only a private repository:

```bash
git commit -m "Prepare research simulator for private Windows transfer"
git branch -M main
gh repo create triangular_arbitrage_system --private --source . --remote origin --push
```

If `gh` is unavailable, create an empty **private** repository in the GitHub
web interface, then use the private repository URL shown there:

```bash
git commit -m "Prepare research simulator for private Windows transfer"
git branch -M main
git remote add origin https://github.com/<owner>/triangular_arbitrage_system.git
git push -u origin main
```

Confirm the repository visibility is **Private** on GitHub before cloning it on
Windows. Never change `--private` to `--public`, never commit `.env`, and never
put a personal access token in the remote URL. The complete Windows clone and
experiment procedure is in [docs/windows_runbook.md](docs/windows_runbook.md).

## Public Binance API use

Only these public endpoints are used:

- `GET /api/v3/exchangeInfo`
- `GET /api/v3/ticker/24hr`
- `GET /api/v3/depth`
- public combined `{symbol}@depth@100ms` WebSocket streams

References: [Binance Spot general endpoints](https://developers.binance.com/docs/binance-spot-api-docs/rest-api/general-endpoints),
[market-data endpoints](https://developers.binance.com/docs/binance-spot-api-docs/rest-api/market-data-endpoints),
[WebSocket market streams](https://developers.binance.com/docs/binance-spot-api-docs/web-socket-streams),
and [market-data-only hosts](https://github.com/binance/binance-spot-api-docs/blob/master/faqs/market_data_only.md).

## MEXC API use

The MEXC adapter only permits these public endpoints:

- `GET /api/v3/exchangeInfo`
- `GET /api/v3/ticker/24hr`
- `GET /api/v3/ticker/bookTicker`
- `GET /api/v3/depth`
- public `spot@public.aggre.depth.v3.api.pb@100ms@<SYMBOL>` WebSocket channels

The official WebSocket schema is included through protobuf descriptors. One
MEXC connection is capped at 30 subscriptions by the adapter, so the 50-symbol
profile is sharded rather than sent as one oversized subscription.

Separately, `tri_arb.tools.check_mexc_fees` permits exactly one authenticated
endpoint:

- signed read-only `GET /api/v3/tradeFee`

That client rejects every other path and method. In particular, the project
contains no order, cancel, balance, transfer, withdrawal, or private execution
request. Credentials are read from environment variables or local `.env`,
redacted from representations/errors, and never written into fee output or
reports.

References: [MEXC Spot V3 API documentation](https://mexcdevelop.github.io/apidocs/spot_v3_en/),
[official protobuf schemas](https://github.com/mexcdevelop/websocket-proto),
[aggregate-depth schema](https://github.com/mexcdevelop/websocket-proto/blob/main/PublicAggreDepthsV3Api.proto),
and [wrapper schema](https://github.com/mexcdevelop/websocket-proto/blob/main/PushDataV3ApiWrapper.proto).

## Limitations

- Displayed liquidity can vanish, be hidden, or be consumed ahead of a real
  order; a local book is an observation, not a fill guarantee.
- Three real orders are not atomic. This simulator reports the route as a cycle,
  but live execution would carry leg risk and inventory risk.
- REST snapshots, network scheduling, Python's event loop, and the observer's
  location all bias latency measurements.
- A quantity haircut and adverse-price buffer are heuristics, not a queue model.
- Common exchange filters are checked, but account-specific restrictions and
  exact matching-engine rounding cannot be reproduced without trading.
- Quote volumes across assets are proxies; ranking quality can change with the
  market and deserves periodic review.
- A short run is statistically weak. Capture multiple regimes and inspect book
  health before drawing a conclusion.
- Long experiments generate substantial disk IO and can fail if the laptop
  sleeps, loses network access, or runs out of storage. Keep it plugged in and
  use checkpoint files to verify progress.

## Sensible next steps

If repeated conservative runs return `PROCEED`, extend research with longer
capture windows, colocated measurements, replayable raw depth archives,
fee-tier sensitivity, queue/fill-probability modeling, and explicit inventory
risk. Keep any future execution system in a separate repository and behind an
independent safety review; this observer should remain incapable of trading.
