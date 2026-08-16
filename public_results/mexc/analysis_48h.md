# MEXC 48-hour triangular-arbitrage research analysis

Source run: `20260803T224851Z_94fc894d`

Final report generated: 2026-08-05 22:49:25 UTC

Scope: offline analysis of the final Markdown/CSV artifacts and the last five checkpoint reports. No raw JSONL or credentials were inspected.

## Executive decision

**UNCLEAR_BUT_DO_NOT_TRADE**

The experiment found rare technically positive observations: 26 of 7,329 signals survived the supplied fees, 23 survived displayed depth, 4 survived the pessimistic model, and 21/20 were still positive at the 50/100 ms rechecks. Those observations prevent the strict all-negative STOP rule from applying mechanically.

However, the evidence does not justify `CONTINUE_TO_7D`: total diagnostic estimated PnL was **-474.05534897**, average and median realistic edges were **-0.1344% / -0.0989%**, ghost arbitrage was **99.71%**, only one cycle had repeated positive evidence, and **none of 47 checkpoints** met the positive-evidence gate. The rare positive observations were overwhelmed by persistent negative economics. This is not evidence for live trading.

Operational decision: **do not run the broad 10-cycle MEXC experiment for seven days**. If more research is desired, use a short targeted full-mode diagnostic on the two USDF/BTC directions to inspect the four pessimistic survivors in detail; do not treat that diagnostic as a trading test.

## Key experiment settings

| Setting | Value |
|---|---:|
| Exchange | MEXC Spot |
| Run duration | 2,880 minutes (48 hours) |
| Storage mode | compact |
| Data directory | `<external-data-dir>` |
| Monitored symbols | 12 |
| Monitored cycles | 10 |
| Fee source | `sanitized_numeric_fee_schedule` |
| Maximum/fallback taker fee per leg | 0.0005 (0.05000%) |
| Fee-schedule symbols loaded / used in monitored set | 7 / 4 |
| Start sizes | 10, 25, 50, 100 USDT |
| Latency buckets | 50, 100, 250, 500, 1,000 ms |
| Maximum cycles requested | 20 |
| Cycles actually monitored | 10 |
| Checkpoint interval | 60 minutes |
| Compact raw sample rate | 0.001 |
| Compact top-N retention | 1,000 |
| Near-break-even retention threshold | -0.0005 |

## Core results

| Metric | Result |
|---|---:|
| Unique raw opportunities | 7,329 |
| Raw opportunity scan observations | 114,107 |
| Profitable after fees | 26 (0.355%) |
| Profitable after displayed depth | 23 (0.314%) |
| Profitable under pessimistic model | 4 (0.0546%) |
| Profitable after 50 ms | 21 / 7,329 (0.29%) |
| Profitable after 100 ms | 20 / 7,329 (0.27%) |
| Average realistic edge | -0.1344% |
| Median realistic edge | -0.0989% |
| Best realistic/net edge | 0.025729% |
| Average estimated PnL | -0.06468213 |
| Median estimated PnL | -0.04174258 |
| Total diagnostic estimated PnL | -474.05534897 |
| Ghost arbitrage | 99.7135% (7,308 / 7,329) |
| Resyncs | 18 |
| Sequence gaps | 18 |
| Scanner deadline misses | 1,297 / 2,975,244 scans (0.0436%) |
| Average book staleness | 349.34 ms |
| Repeated positive cycles | 1 |
| Positive checkpoints | 0 / 47 |

The large difference between raw observations and pessimistic survivors is decisive. Only 0.0546% of unique signals survived the pessimistic model, while the cumulative realistic PnL remained deeply negative.

## Checkpoint trend

The final five hourly checkpoints show late but non-persistent improvements in fee/depth/latency counts, no new pessimistic survivors, and worsening cumulative PnL.

| Elapsed | Raw | Fees | Depth | Pessimistic | 50 ms | 100 ms | Ghosts | Total PnL | Resync/gaps | Staleness |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 2,580.50 min | 6,159 | 22 | 19 | 4 | 17 | 16 | 99.72% | -431.2655 | 18 / 18 | 365.21 ms |
| 2,640.52 min | 7,277 | 22 | 19 | 4 | 17 | 16 | 99.77% | -471.0138 | 18 / 18 | 348.91 ms |
| 2,700.55 min | 7,285 | 22 | 19 | 4 | 17 | 16 | 99.77% | -471.7935 | 18 / 18 | 348.95 ms |
| 2,760.57 min | 7,309 | 26 | 23 | 4 | 21 | 20 | 99.71% | -472.9177 | 18 / 18 | 349.15 ms |
| 2,820.60 min | 7,329 | 26 | 23 | 4 | 21 | 20 | 99.71% | -474.0553 | 18 / 18 | 349.34 ms |

Interpretation:

- Four new fee/depth survivors and four new 50/100 ms survivors appeared between approximately hour 45 and hour 46.
- Pessimistic survivors remained fixed at four; the strongest conservative evidence did not improve.
- The last hour added no new unique signals or survivors.
- Total PnL deteriorated by about 42.79 units over these checkpoints.
- Resyncs and sequence gaps stayed at 18/18, so data quality did not progressively degrade in the final hours.
- Ghost share briefly worsened to 99.77%, then returned to 99.71%; this is not a meaningful economic improvement.

## Fee analysis

The maximum/fallback supplied taker fee was **0.0005 per leg (0.05000%)**. Symbol-specific fees ranged from zero to 0.0005, so the actual fee-aware scanner could produce 26 survivors while the equal-fee sensitivity table produced 24 at 0.0005.

| Fee metric | Value |
|---|---:|
| Average break-even fee per leg | 0.00994225% |
| Median break-even fee per leg | 0.00768910% |
| P95 break-even fee per leg | 0.02442382% |
| P99 break-even fee per leg | 0.03976084% |
| Maximum break-even fee per leg | 0.05857066% |
| Maximum raw edge | 0.17591801% |
| Best net edge after fees | 0.02572925% |

The median raw signal could tolerate only about 0.00769% per leg, far below the 0.05% fallback fee. Fee-only sensitivity was:

| Equal taker fee per leg | Fee-only profitable | Share |
|---:|---:|---:|
| 0.10000% | 0 | 0.0000% |
| 0.05000% | 24 | 0.3275% |
| 0.02000% | 993 | 13.5489% |
| 0.01000% | 3,037 | 41.4381% |
| 0.00000% | 7,329 | 100.0000% |

Lower fees would materially increase theoretical fee-only survivors, but this table does not replay displayed depth, pessimistic haircut/slippage, or latency. It therefore cannot reverse the research decision. The current strategy is predominantly viable only under fee assumptions substantially below the supplied fallback; even at actual symbol-specific fees, realistic aggregate PnL was strongly negative.

## Best opportunities analysis

The supplied `top_opportunities` CSV contains exactly the 20 highest-raw-edge rows, not independent global top-20 exports for net edge and realistic PnL. The net and PnL rankings below are re-sorts of those same 20 candidates. Tied rows at different sizes often represent one market state, so they must not be counted as independent market events.

### Top 20 by raw edge

| # | Cycle | Size | Raw edge | Net edge | Realistic PnL |
|---:|---|---:|---:|---:|---:|
| 1 | USDT:USDF:BTC:USDT | 10 | 0.175918% | 0.025729% | 0.002573 |
| 2 | USDT:USDF:BTC:USDT | 25 | 0.175918% | 0.025729% | 0.006432 |
| 3 | USDT:USDF:BTC:USDT | 50 | 0.175918% | 0.025729% | 0.012865 |
| 4 | USDT:USDF:BTC:USDT | 100 | 0.175918% | 0.025729% | 0.025729 |
| 5 | USDT:USDF:BTC:USDT | 100 | 0.159470% | 0.009306% | 0.009306 |
| 6 | USDT:USDF:BTC:USDT | 10 | 0.159392% | 0.009228% | 0.000923 |
| 7 | USDT:USDF:BTC:USDT | 25 | 0.159392% | 0.009228% | 0.002307 |
| 8 | USDT:USDF:BTC:USDT | 50 | 0.159392% | 0.009228% | 0.004614 |
| 9 | USDT:BTC:USDF:USDT | 10 | 0.158204% | 0.007993% | 0.000799 |
| 10 | USDT:BTC:USDF:USDT | 25 | 0.158204% | 0.008024% | 0.002006 |
| 11 | USDT:BTC:USDF:USDT | 50 | 0.158204% | 0.008034% | 0.004017 |
| 12 | USDT:BTC:USDF:USDT | 100 | 0.158204% | 0.008039% | 0.008039 |
| 13 | USDT:BTC:USDF:USDT | 10 | 0.157204% | 0.007001% | 0.000700 |
| 14 | USDT:BTC:USDF:USDT | 25 | 0.157204% | 0.007028% | 0.001757 |
| 15 | USDT:BTC:USDF:USDT | 50 | 0.157204% | 0.007037% | 0.003518 |
| 16 | USDT:USDF:BTC:USDT | 10 | 0.155794% | 0.005635% | 0.000564 |
| 17 | USDT:USDF:BTC:USDT | 25 | 0.155794% | 0.005635% | -0.005581 |
| 18 | USDT:USDF:BTC:USDT | 50 | 0.155794% | 0.005635% | -0.025218 |
| 19 | USDT:USDF:BTC:USDT | 100 | 0.155794% | 0.005635% | -0.064493 |
| 20 | USDT:BTC:USDF:USDT | 100 | 0.153705% | 0.003549% | 0.003549 |

### Top 20 by net edge within the exported candidate set

| Rank group | Rows | Net edge |
|---:|---|---:|
| 1-4 | USDT:USDF:BTC:USDT, sizes 10/25/50/100 | 0.025729% |
| 5 | USDT:USDF:BTC:USDT, size 100 | 0.009306% |
| 6-8 | USDT:USDF:BTC:USDT, sizes 10/25/50 | 0.009228% |
| 9 | USDT:BTC:USDF:USDT, size 100 | 0.008039% |
| 10 | USDT:BTC:USDF:USDT, size 50 | 0.008034% |
| 11 | USDT:BTC:USDF:USDT, size 25 | 0.008024% |
| 12 | USDT:BTC:USDF:USDT, size 10 | 0.007993% |
| 13 | USDT:BTC:USDF:USDT, size 50 | 0.007037% |
| 14 | USDT:BTC:USDF:USDT, size 25 | 0.007028% |
| 15 | USDT:BTC:USDF:USDT, size 10 | 0.007001% |
| 16-19 | USDT:USDF:BTC:USDT, sizes 10/25/50/100 | 0.005635% |
| 20 | USDT:BTC:USDF:USDT, size 100 | 0.003549% |

### Top 20 by realistic PnL within the exported candidate set

| # | Cycle | Size | Realistic PnL |
|---:|---|---:|---:|
| 1 | USDT:USDF:BTC:USDT | 100 | 0.025729 |
| 2 | USDT:USDF:BTC:USDT | 50 | 0.012865 |
| 3 | USDT:USDF:BTC:USDT | 100 | 0.009306 |
| 4 | USDT:BTC:USDF:USDT | 100 | 0.008039 |
| 5 | USDT:USDF:BTC:USDT | 25 | 0.006432 |
| 6 | USDT:USDF:BTC:USDT | 50 | 0.004614 |
| 7 | USDT:BTC:USDF:USDT | 50 | 0.004017 |
| 8 | USDT:BTC:USDF:USDT | 100 | 0.003549 |
| 9 | USDT:BTC:USDF:USDT | 50 | 0.003518 |
| 10 | USDT:USDF:BTC:USDT | 10 | 0.002573 |
| 11 | USDT:USDF:BTC:USDT | 25 | 0.002307 |
| 12 | USDT:BTC:USDF:USDT | 25 | 0.002006 |
| 13 | USDT:BTC:USDF:USDT | 25 | 0.001757 |
| 14 | USDT:USDF:BTC:USDT | 10 | 0.000923 |
| 15 | USDT:BTC:USDF:USDT | 10 | 0.000799 |
| 16 | USDT:BTC:USDF:USDT | 10 | 0.000700 |
| 17 | USDT:USDF:BTC:USDT | 10 | 0.000564 |
| 18 | USDT:USDF:BTC:USDT | 25 | -0.005581 |
| 19 | USDT:USDF:BTC:USDT | 50 | -0.025218 |
| 20 | USDT:USDF:BTC:USDT | 100 | -0.064493 |

All 20 exported rows came from only two directions: 12 rows (60%) from `USDT:USDF:BTC:USDT` and 8 rows (40%) from `USDT:BTC:USDF:USDT`. This concentration is useful for diagnostics but weakens claims of broad repeatability. Seventeen of the 20 rows have positive reported depth-aware estimated PnL. The CSV does not contain a pessimistic-survival flag, so the four global pessimistic survivors cannot be attributed to individual exported rows without additional retained signal-level evidence. No such attribution is assumed here.

## Cycle-level analysis

Active cycles ranked by total realistic PnL from least negative to worst:

| Rank | Cycle | Signals | Total PnL | Avg PnL | Max raw edge | Max net edge | Ghosts | Classification |
|---:|---|---:|---:|---:|---:|---:|---:|---|
| 1 | USDT:BTC:USD1:USDT | 152 | -5.4504 | -0.035858 | 0.086407% | -0.013580% | 100.00% | noisy |
| 2 | USDT:HBAR:USDC:USDT | 297 | -31.4929 | -0.106037 | 0.053458% | -0.046570% | 100.00% | noisy |
| 3 | USDT:BTC:USDF:USDT | 1,042 | -50.6818 | -0.048639 | 0.158204% | 0.008039% | 99.23% | promising but not executable |
| 4 | USDT:USDF:BTC:USDT | 1,168 | -61.5107 | -0.052663 | 0.175918% | 0.025729% | 98.89% | promising but not executable |
| 5 | USDT:USD1:BTC:USDT | 2,662 | -104.1062 | -0.039108 | 0.090884% | -0.009182% | 100.00% | noisy |
| 6 | USDT:USDC:HBAR:USDT | 2,008 | -220.8133 | -0.109967 | 0.102312% | 0.002147% | 100.00% | promising but not executable |

Dead cycles with zero signals were `USDT:DOGE:USD1:USDT`, `USDT:TRX:USD1:USDT`, `USDT:USD1:DOGE:USDT`, and `USDT:USD1:TRX:USDT`.

No cycle qualifies as genuinely promising: every active cycle had negative total and average PnL, and all ghost rates were at least 98.89%. The USDF/BTC directions are the only sensible candidates for a short targeted diagnostic because they dominate the best-edge export and have the lowest ghost rates, but they remain economically negative overall.

## Latency analysis

| Target | Observed | Profitable | Survival | Avg actual | Late checks |
|---:|---:|---:|---:|---:|---:|
| 50 ms | 7,329 | 21 | 0.29% | 55.7 ms | 572 |
| 100 ms | 7,329 | 20 | 0.27% | 101.7 ms | 21 |
| 250 ms | 7,329 | 14 | 0.19% | 251.5 ms | 121 |
| 500 ms | 7,329 | 15 | 0.20% | 501.2 ms | 91 |
| 1,000 ms | 7,329 | 16 | 0.22% | 1,007.0 ms | 961 |

Latency was not the primary failure. Fees eliminated all but 26 signals; displayed depth left 23, and the pessimistic model left only 4. The 50/100 ms stages retained 21/20 of the depth-positive population. Thus fees were the first major filter and conservative execution assumptions were the next major filter. Latency reduced the already tiny survivor set but did not create the dominant loss.

Survival is not perfectly monotonic at later buckets because an opportunity can disappear and reappear as books change; the 1,000 ms count should not be interpreted as a longer guaranteed lifetime. Median opportunity lifetime was zero milliseconds.

## Data quality analysis

- Four of 12 books were unhealthy after the startup wait: BTCUSDF, TRXUSD1, USDCUSDT, and USDFUSDT. Unhealthy cycles were excluded until synchronized.
- There were 18 resyncs and 18 sequence gaps over 48 hours. Both counts were unchanged over the last five checkpoints, indicating no late-run deterioration.
- Scanner misses were 1,297 of 2,975,244 scans (0.0436%). Mean scan time was 1.43 ms versus a 50 ms cadence; this is acceptable for the broad conclusion.
- Average book staleness was 349.34 ms, below the configured 2,000 ms unhealthy cutoff.
- Latency coverage was 100%, but 1,766 of 36,645 checks (4.82%) exceeded the 25 ms lateness tolerance and maximum lateness was 110 ms. Timing precision was therefore imperfect.
- No malformed JSONL records were reported, and cumulative counts/extrema were exact. Percentiles and medians used bounded 4,096-record samples.

The data is good enough to trust the broad negative economic conclusion because losses, ghost share, and checkpoint persistence are far from the continuation threshold. It is not strong enough to claim that the four rare pessimistic survivors are deployable: startup health issues, scheduler lateness, compact retention, and the lack of per-row pessimistic flags call for targeted diagnostics before any further inference.

## 48h to 7d decision

Strict-rule evaluation:

| Rule set | Result |
|---|---|
| STOP | Not all listed STOP conditions hold: depth, pessimistic, and 50/100 ms counts are nonzero. Ghost rate >99% and total PnL <=0 do hold. |
| CONTINUE_TO_7D | Fails. Although depth/pessimistic/latency survivors exist and one repeated positive cycle was recorded, realistic total PnL is negative and zero checkpoints passed the positive-evidence gate. |
| UNCLEAR_BUT_DO_NOT_TRADE | Applies. A few positive observations exist, but they are too rare, concentrated, noisy, and economically overwhelmed to justify continuation or trading. |

Decision: **UNCLEAR_BUT_DO_NOT_TRADE**, with an operational recommendation to **stop the broad MEXC seven-day continuation**.

## Final recommendation

1. **Do not run the current broad MEXC configuration for seven days.** Forty-eight hours produced strongly negative aggregate economics and no positive checkpoints.
2. **Do not trade these signals.** The run is a paper simulation, 99.71% of signals were ghosts, and the positive subset is too sparse.
3. **Consider another exchange only as a new research hypothesis**, prioritizing lower verified taker fees, deeper books, and reliable public depth sequencing. Apply the same fee/depth/pessimistic/latency gates before comparison.
4. **Consider a less fee- and latency-sensitive strategy type** rather than broad spot triangular scanning at this cadence. Slower-horizon relative-value or cross-venue research may offer more room for fees and execution uncertainty, but requires separate safety and data validation.
5. **Reduce scope if diagnosing MEXC further.** Focus on `USDT:USDF:BTC:USDT` and `USDT:BTC:USDF:USDT`; the rest were dead or uniformly negative/noisy.
6. **A short targeted full-mode run is reasonable for diagnosis only**, to identify which records survived pessimistic and latency gates and whether they repeat as independent market events. It is not a substitute for a profitable 48-hour validation and is not authorization for live trading.
