# Binance Spot experiment summary

This summary reproduces the final Binance metrics recorded in the project
documentation. The original generated report is not present in the current
checkout, so fields that cannot be verified from that record are marked as not
retained.

## Experiment

| Metric | Value |
|---|---:|
| Exchange | Binance Spot |
| Duration | 60 minutes |
| Symbols monitored | 39 |
| Directional cycles monitored | 50 |
| Taker-fee assumption per leg | 0.001 (0.10%) |
| Raw opportunities | 152,920 |
| Profitable after fees | 0 |
| Profitable after displayed depth | 0 |
| Pessimistic survivors | 0 |
| Ghost arbitrage | 100% |
| Total simulated PnL | not retained |
| Decision | STOP |

The observed raw top-of-book discrepancies did not cover the configured taker
fees. Because no signal survived fees or displayed depth, the experiment did
not support further execution research under that broad configuration.
