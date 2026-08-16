# MEXC Spot Triangular Arbitrage Simulation Report

Generated: 2026-08-05T22:49:25.375607+00:00

> Research-only paper simulation. This report does not prove live profitability.

> Cumulative streaming report: counts, sums, and extrema are exact since process start; percentiles and medians use a bounded deterministic sample.

## Executive summary

| Metric | Value |
|---|---:|
| Exchange | MEXC Spot |
| Maximum/fallback taker fee per leg | 0.05000% |
| Fee source | sanitized_numeric_fee_schedule |
| Numeric fee schedule symbols loaded | 7 |
| Monitored symbols with supplied fees | 4 |
| Symbol-specific taker fee range | 0.00000% to 0.05000% |
| Numeric fee schedule generated | 2026-08-01T00:05:14.804225+00:00 |
| Run duration | 2,880.00 minutes |
| Symbols monitored | 12 |
| Cycles monitored | 10 |
| Unique raw opportunity signals | 7329 |
| Raw opportunity scan observations | 114107 |
| Profitable after fees | 26 |
| Profitable after displayed depth | 23 |
| Profitable under pessimistic model | 4 / 7329 observed |
| Non-executable / filter-rejected signals | 0 / 0 |
| Slowest-bucket latency coverage | 7329 / 7329 signals (100.00%) |
| Latency scheduling tolerance / maximum lateness | 25.0 / 110.0 ms |
| Scanner deadline misses | 1297 |
| Average / median realistic edge | -0.1344% / -0.0989% |
| Best net edge after fees | 0.0257% |
| Average / median estimated PnL | -0.06468213 / -0.04174258 |
| Total diagnostic estimated PnL | -474.05534897 |
| Ghost arbitrage | 99.71% (7308) |
| Average book staleness at signal detection | 349.34 ms |
| Resyncs / sequence gaps | 18 / 18 |
| Positive periodic checkpoints | 0 / 47 |

## Diagnostics

### Raw and net edge percentiles

Net edge is the recorded edge after the configured taker fee on all three legs.

Online percentile and median values use a reservoir of 4,096 sampled signals out of 7,329 cumulative signals (limit 4,096). The Samples column is the exact cumulative population; minima, maxima, counts, sums, and averages remain exact.

| Stage | Samples | Min | P50 | P90 | P95 | P99 | P99.9 | Max |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Raw top-of-book | 7329 | 0.00002390% | 0.02307084% | 0.06287295% | 0.07330728% | 0.11937745% | 0.15820356% | 0.17591801% |
| Net after fees | 7329 | -0.14980083% | -0.08356892% | -0.04894539% | -0.04303735% | -0.02717383% | 0.00803862% | 0.02572925% |

### Break-even taker fee per leg

For each non-negative raw edge, this estimates the largest equal taker fee on each of three legs that leaves the theoretical cycle at break-even: `(1 + raw_edge) * (1 - fee) ** 3 = 1`.

| Samples | Average | Median | P95 | P99 | Maximum |
|---:|---:|---:|---:|---:|---:|
| 7329 | 0.00994225% | 0.00768910% | 0.02442382% | 0.03976084% | 0.05857066% |

### Fee sensitivity

This is a theoretical fee-only recomputation from each raw three-leg multiplier. It does not replay lot-size rounding, filters, displayed depth, or latency.

| Taker fee per leg | Fee percent | Profitable | Samples | Share |
|---:|---:|---:|---:|---:|
| 0.001 | 0.10000% | 0 | 7329 | 0.0000% |
| 0.0005 | 0.05000% | 24 | 7329 | 0.3275% |
| 0.0002 | 0.02000% | 993 | 7329 | 13.5489% |
| 0.0001 | 0.01000% | 3037 | 7329 | 41.4381% |
| 0.0 | 0.00000% | 7329 | 7329 | 100.0000% |

### Best 20 raw opportunities

| Cycle | Start size | Raw edge | Edge after fees | Estimated PnL | Limiting leg | Book staleness |
|---|---:|---:|---:|---:|---|---:|
| USDT:USDF:BTC:USDT | 10 | 0.17591801% | 0.02572925% | 0.00257293 | USDFUSDT | 353.0 ms |
| USDT:USDF:BTC:USDT | 25 | 0.17591801% | 0.02572925% | 0.00643231 | USDFUSDT | 433.0 ms |
| USDT:USDF:BTC:USDT | 50 | 0.17591801% | 0.02572925% | 0.01286463 | USDFUSDT | 457.0 ms |
| USDT:USDF:BTC:USDT | 100 | 0.17591801% | 0.02572925% | 0.02572925 | USDFUSDT | 482.0 ms |
| USDT:USDF:BTC:USDT | 100 | 0.15946995% | 0.00930585% | 0.00930585 | USDFUSDT | 950.0 ms |
| USDT:USDF:BTC:USDT | 10 | 0.15939170% | 0.00922772% | 0.00092277 | USDFUSDT | 807.0 ms |
| USDT:USDF:BTC:USDT | 25 | 0.15939170% | 0.00922772% | 0.00230693 | USDFUSDT | 890.0 ms |
| USDT:USDF:BTC:USDT | 50 | 0.15939170% | 0.00922772% | 0.00461386 | USDFUSDT | 919.0 ms |
| USDT:BTC:USDF:USDT | 10 | 0.15820356% | 0.00799270% | 0.00079927 | BTCUSDT | 1,196.0 ms |
| USDT:BTC:USDF:USDT | 25 | 0.15820356% | 0.00802364% | 0.00200591 | BTCUSDT | 1,244.0 ms |
| USDT:BTC:USDF:USDT | 50 | 0.15820356% | 0.00803396% | 0.00401698 | BTCUSDT | 1,276.0 ms |
| USDT:BTC:USDF:USDT | 100 | 0.15820356% | 0.00803911% | 0.00803911 | BTCUSDT | 1,312.0 ms |
| USDT:BTC:USDF:USDT | 10 | 0.15720395% | 0.00700070% | 0.00070007 | BTCUSDT | 65.0 ms |
| USDT:BTC:USDF:USDT | 25 | 0.15720395% | 0.00702780% | 0.00175695 | BTCUSDT | 96.0 ms |
| USDT:BTC:USDF:USDT | 50 | 0.15720395% | 0.00703684% | 0.00351842 | BTCUSDT | 96.0 ms |
| USDT:USDF:BTC:USDT | 10 | 0.15579386% | 0.00563528% | 0.00056353 | USDFUSDT | 127.0 ms |
| USDT:USDF:BTC:USDT | 25 | 0.15579386% | 0.00563528% | -0.00558108 | USDFUSDT | 143.0 ms |
| USDT:USDF:BTC:USDT | 50 | 0.15579386% | 0.00563528% | -0.02521850 | USDFUSDT | 159.0 ms |
| USDT:USDF:BTC:USDT | 100 | 0.15579386% | 0.00563528% | -0.06449333 | USDFUSDT | 175.0 ms |
| USDT:BTC:USDF:USDT | 100 | 0.15370548% | 0.00354919% | 0.00354919 | BTCUSDT | 128.0 ms |

A machine-readable copy is saved beside this report as `top_opportunities_<report-id>.csv`.

### Per-cycle diagnostics

| Cycle | Signals | Avg raw edge | Max raw edge | Avg net edge | Best net edge | Ghosts |
|---|---:|---:|---:|---:|---:|---:|
| USDT:BTC:USD1:USDT | 152 | 0.022915% | 0.086407% | -0.076910% | -0.013580% | 100.00% |
| USDT:BTC:USDF:USDT | 1042 | 0.047826% | 0.158204% | -0.102030% | 0.008039% | 99.23% |
| USDT:DOGE:USD1:USDT | 0 | n/a | n/a | n/a | n/a | n/a |
| USDT:HBAR:USDC:USDT | 297 | 0.025752% | 0.053458% | -0.074248% | -0.046570% | 100.00% |
| USDT:TRX:USD1:USDT | 0 | n/a | n/a | n/a | n/a | n/a |
| USDT:USD1:BTC:USDT | 2662 | 0.020192% | 0.090884% | -0.079803% | -0.009182% | 100.00% |
| USDT:USD1:DOGE:USDT | 0 | n/a | n/a | n/a | n/a | n/a |
| USDT:USD1:TRX:USDT | 0 | n/a | n/a | n/a | n/a | n/a |
| USDT:USDC:HBAR:USDT | 2008 | 0.025376% | 0.102312% | -0.071500% | 0.002147% | 100.00% |
| USDT:USDF:BTC:USDT | 1168 | 0.045379% | 0.175918% | -0.104614% | 0.025729% | 98.89% |

## Latency survival

| Target | Avg actual | Median lateness | Profitable | Observed | Survival | Late checks |
|---:|---:|---:|---:|---:|---:|---:|
| 50 ms | 55.7 ms | -3.0 ms | 21 | 7329 | 0.29% | 572 |
| 100 ms | 101.7 ms | -6.0 ms | 20 | 7329 | 0.27% | 21 |
| 250 ms | 251.5 ms | 0.0 ms | 14 | 7329 | 0.19% | 121 |
| 500 ms | 501.2 ms | 0.0 ms | 15 | 7329 | 0.20% | 91 |
| 1000 ms | 1,007.0 ms | 0.0 ms | 16 | 7329 | 0.22% | 961 |

## Opportunity lifetime

| Minimum | Median | P90 | Maximum |
|---:|---:|---:|---:|
| 0.0 ms | 0.0 ms | 0.0 ms | 1,000.0 ms |

## Best cycles by realistic PnL

| Cycle | Signals | Total PnL | Average PnL | Ghosts |
|---|---:|---:|---:|---:|
| USDT:BTC:USD1:USDT | 152 | -5.45042772 | -0.03585808 | 100.00% |
| USDT:HBAR:USDC:USDT | 297 | -31.49288480 | -0.10603665 | 100.00% |
| USDT:BTC:USDF:USDT | 1042 | -50.68177635 | -0.04863894 | 99.23% |
| USDT:USDF:BTC:USDT | 1168 | -61.51068161 | -0.05266325 | 98.89% |
| USDT:USD1:BTC:USDT | 2662 | -104.10623204 | -0.03910828 | 100.00% |

## Worst cycles by realistic PnL

| Cycle | Signals | Total PnL | Average PnL | Ghosts |
|---|---:|---:|---:|---:|
| USDT:USDC:HBAR:USDT | 2008 | -220.81334646 | -0.10996681 | 100.00% |
| USDT:USD1:BTC:USDT | 2662 | -104.10623204 | -0.03910828 | 100.00% |
| USDT:USDF:BTC:USDT | 1168 | -61.51068161 | -0.05266325 | 98.89% |
| USDT:BTC:USDF:USDT | 1042 | -50.68177635 | -0.04863894 | 99.23% |
| USDT:HBAR:USDC:USDT | 297 | -31.49288480 | -0.10603665 | 100.00% |

## Noisy cycles

| Cycle | Signals | Total PnL | Average PnL | Ghosts |
|---|---:|---:|---:|---:|
| USDT:USD1:BTC:USDT | 2662 | -104.10623204 | -0.03910828 | 100.00% |
| USDT:USDC:HBAR:USDT | 2008 | -220.81334646 | -0.10996681 | 100.00% |
| USDT:HBAR:USDC:USDT | 297 | -31.49288480 | -0.10603665 | 100.00% |
| USDT:BTC:USD1:USDT | 152 | -5.45042772 | -0.03585808 | 100.00% |
| USDT:BTC:USDF:USDT | 1042 | -50.68177635 | -0.04863894 | 99.23% |

## Storage

- data_dir: <external-data-dir>
- data_drive: sanitized
- storage_mode: compact
- min_free_gib: 15.0
- free_gib_at_start: omitted
- raw_signal_sample_rate: 0.001
- top_n_retention: 1000
- near_break_even_threshold: -0.0005
- checkpoint_interval_minutes: 60.0
- compact_mode_active: True
- latest_report_path: public_results/mexc/report_48h.md

## Assumptions

- Exchange: MEXC Spot
- Market Data Only: True
- Research Only: True
- Live Trading Available: False
- Root Asset: USDT
- Fee Source: sanitized_numeric_fee_schedule
- Fee Rate Per Taker Leg: 0.0005
- Fee Fallback Is Maximum Observed: True
- Fee Schedule Generated At: 2026-08-01T00:05:14.804225+00:00
- Fee Schedule Symbol Count: 7
- Reported Symbol Fee Count: 4
- Fee Schedule Minimum Taker Fee: 0
- Fee Schedule Maximum Taker Fee: 0.0005
- Symbol Taker Fees: {"TRXUSD1": "0", "TRXUSDT": "0.0005", "USD1USDT": "0", "USDCUSDT": "0"}
- Fee Sensitivity Rates: ["0.001", "0.0005", "0.0002", "0.0001", "0"]
- Fee Assumptions Are Simulated: True
- Mexc Fee Verification Required: True
- Start Sizes: ["10", "25", "50", "100"]
- Latency Buckets Ms: [50, 100, 250, 500, 1000]
- Scan Interval Ms: 50
- Signal Cooldown Ms: 1000
- Stored Depth Levels: 100
- Snapshot Limit: 100
- Depth Stream Interval Ms: 100
- Displayed Quantity Haircut: 0.25
- Extra Slippage Bps: 1
- Book Stale After Ms: 2000
- Min Quote Volume Usdt: 100000
- Max Spread Bps: 50
- Min Top Of Book Notional: 50
- Exclude Assets: []
- Exclude Symbol Patterns: ["*3L*", "*3S*", "*5L*", "*5S*", "*BULL*", "*BEAR*"]
- Rest Base Url: https://api.mexc.com
- Websocket Base Url: wss://wbs-api.mexc.com/ws
- Data Dir: <external-data-dir>
- Data Drive: D:
- Storage Mode: compact
- Min Free Gib: 15.0
- Free Gib At Start: omitted
- Raw Signal Sample Rate: 0.001
- Top N Retention: 1000
- Near Break Even Threshold: -0.0005
- Checkpoint Interval Minutes: 60.0
- Compact Mode Active: True
- Latest Report Path: public_results/mexc/report_48h.md

## Data-quality warnings

- **Scanner deadline misses:** 1297 of 2975244 scans (0.04%); mean scan duration 1.43 ms and maximum 231.90 ms. A miss means local cycle evaluation exceeded its configured cadence; it reduces sampling frequency but is not itself a dropped exchange message.
- **Average book staleness at signal detection:** 349.34 ms across 7329 samples; configured unhealthy cutoff 2,000.0 ms. Staleness is time since the local book last changed, not one-way network latency; unhealthy books are excluded from simulation.
- **Maximum latency lateness:** 110.00 ms; 1766 of 36645 checks (4.8192%) exceeded the 25.0 ms tolerance. Lateness is local scheduler overshoot beyond a target recheck time, not exchange response latency.
- **Unhealthy books at startup:** 4 of 12 (BTCUSDF, TRXUSD1, USDCUSDT, USDFUSDT; captured after the startup wait). Cycles requiring an unhealthy book are skipped until every required book is synchronized and fresh.

- Records read: 49059
- Malformed JSONL lines skipped: 0
- Decision sample threshold: 20 raw opportunities
- Meaningful latency coverage: yes
- Latency scheduling within tolerance: no
- Repeated positive cycles: 1
- Positive checkpoints toward the 48-hour continuation gate: 0 / 2 required (47 checkpoints published)
- Streaming aggregation: cumulative counts, sums, extrema, fee-sensitivity counts, top opportunities, and per-cycle totals are exact since process start. Percentiles and medians are estimates from at most 4096 signal and latency records each (current samples: 4096 / 4096).

## Conclusion

**UNCLEAR** — The sample or conservative survival evidence is not yet strong enough for a go/no-go decision.

## 48-hour decision

**48H_DECISION: UNCLEAR** — The completed evidence does not satisfy every STOP or CONTINUE_TO_7D condition.

This conclusion concerns further research only. Displayed liquidity can vanish, queue position is unknown, and paper fills do not establish live execution performance.

**MEXC fee warning:** Every fee value in this report is a simulated assumption. Verify applicable exchange fees independently before interpreting the simulation.
