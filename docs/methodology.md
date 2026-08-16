# Methodology

## Cycle construction

A triangular cycle starts with a root asset, performs three spot conversions,
and returns to the same asset. For a route `A -> B -> C -> A`, the raw
top-of-book multiplier is

```text
M_raw = r(A,B) * r(B,C) * r(C,A)
raw_edge = M_raw - 1
```

Each conversion rate depends on direction. Selling base consumes bids; buying
base spends quote currency against asks. Discovery builds only cycles supported
by listed, enabled spot symbols and ranks a bounded set for monitoring.

## Why raw edge is insufficient

A positive best-bid/best-ask product is only a screen. It ignores three taker
fees, quantity and notional filters, price and lot rounding, displayed quantity,
book changes between legs, and execution delay. The scanner records raw edge so
the later rejection stages can be measured rather than hidden.

## Fee model

Fees are deducted from the output of every leg. With one equal taker fee `f`, a
rough fee-only multiplier is

```text
M_fee = M_raw * (1 - f)^3
```

The simulator can use a configured fee, an explicit command-line value, or an
optional local MEXC fee schedule. Published MEXC artifacts retain numeric fee
assumptions but not authenticated responses or account details.

## Depth and exchange filters

The depth model walks displayed bids or asks until the requested input is
converted. It applies exchange quantity, notional, tick, and step constraints,
rounds conservatively, tracks residual inventory, and rejects routes that
cannot complete at the observed depth.

## Latency model

Signals are reevaluated against the local synchronized books after configured
delays, normally 50, 100, 250, 500, and 1,000 milliseconds. The report records
actual elapsed time and scheduler lateness separately. These measurements are
local observer timings, not exchange matching-engine or order round-trip
latency.

## Pessimistic model

The pessimistic pass reduces displayed quantity and applies an adverse price
buffer to later legs. The checked-in profiles use a 25% displayed-quantity
haircut and one basis point of extra slippage. This is a stress model, not a
queue-position or fill-probability model.

## Ghost arbitrage

A ghost is a raw mathematical opportunity that is not executable or does not
remain profitable under fees, depth, filters, pessimistic assumptions, or the
first latency recheck. Ghost share quantifies how often a raw crossed product
fails to become durable paper-execution evidence.

## Decision gates

Reports distinguish research continuation from trading:

- `STOP`: conservative evidence is absent or persistently negative.
- `UNCLEAR`: duration, infrastructure, or survivor evidence is insufficient.
- `CONTINUE_TO_7D`: a completed 48-hour sample contains repeated depth and
  pessimistic survivors, positive latency survival, non-negative aggregate
  realistic PnL, and positive evidence across multiple checkpoints.

No report decision authorizes live trading. This repository contains no
execution client.
