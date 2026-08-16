# Published research results

This directory contains compact, sanitized outputs from the completed Binance
and MEXC Spot paper-simulation experiments.

Included files contain aggregate metrics, report tables, and ranked simulated
opportunities. They are enough to review the research conclusion without the
large event streams produced during collection.

## Contents

```text
public_results/
  binance/
    report.md
    summary.csv
    top_opportunities.csv
  mexc/
    report_48h.md
    summary_48h.csv
    top_opportunities_48h.csv
    analysis_48h.md
    key_metrics_48h.csv
  DATA_NOTICE.md
```

The Binance source report was not retained with the current checkout. Its
public report and summary reproduce the metrics recorded in the project
documentation and explicitly mark unavailable fields. The MEXC files are
sanitized copies of the completed 48-hour artifacts.

## Exclusions

The package intentionally excludes:

- raw and sampled JSONL market observations;
- order-book snapshots and WebSocket messages;
- operational logs and local filesystem paths;
- API credentials, `.env`, signatures, and environment variables;
- authenticated fee-response payloads and generated account configuration;
- checkpoint files that duplicate the final aggregate report.

These are simulation outputs, not trade records. They do not prove live
profitability and should not be interpreted as trading advice.
