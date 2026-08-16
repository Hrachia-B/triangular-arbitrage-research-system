# Experiments

## Binance Spot

The retained project record describes a 60-minute run over 39 symbols and 50
directional cycles using a 0.10% taker-fee assumption per leg. It recorded
152,920 raw opportunities. None survived fees or displayed depth, and sampled
signals were classified as 100% ghost arbitrage. The decision was `STOP`.

The original generated Binance report is not present in this checkout. The
sanitized public summary therefore includes only metrics preserved in the
project documentation and marks total PnL as unavailable.

## MEXC short validation

The final 10-minute compact validation monitored seven symbols and six cycles.
It recorded eight unique raw signals: eight survived the supplied numeric fee
schedule, one survived displayed depth, and none survived the pessimistic model.
Ghost share was 87.5%, so the short-run conclusion remained `UNCLEAR`.

## MEXC 48-hour experiment

The completed run monitored 12 symbols and 10 cycles for 2,880 minutes. Its
maximum/fallback taker-fee assumption was 0.05% per leg.

| Metric | Value |
|---|---:|
| Unique raw signals | 7,329 |
| Profitable after fees | 26 |
| Profitable after displayed depth | 23 |
| Profitable under pessimistic model | 4 |
| Profitable after 50 / 100 ms | 21 / 20 |
| Ghost arbitrage | 99.71% |
| Total diagnostic estimated PnL | -474.05534897 |
| Positive checkpoints | 0 / 47 |

Rare positive observations existed, but aggregate realistic PnL was strongly
negative and no checkpoint satisfied the positive-evidence gate. The public
analysis labels the result `UNCLEAR_BUT_DO_NOT_TRADE` and recommends against a
broad seven-day continuation.

## Interpretation

Fees eliminated most raw signals. Depth and pessimistic execution assumptions
reduced the survivor set further; latency was a secondary filter rather than
the main source of failure. The two USDF/BTC directions dominated the best raw
rows, but both remained negative in aggregate and had ghost rates above 98%.

Under the tested conditions, neither broad Binance nor broad MEXC spot
triangular arbitrage justified live execution.
