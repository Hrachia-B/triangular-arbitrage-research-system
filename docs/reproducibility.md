# Reproducibility

## Supported environment

- Python 3.11 or 3.12; Python 3.12 is recommended.
- Windows 10/11 with Windows PowerShell 5.1 or PowerShell 7.
- macOS and Linux with a POSIX shell.

Exact environments used during development are listed in
[`environment_versions.md`](environment_versions.md). Dependency ranges are in
`requirements.txt` and `pyproject.toml`.

## Install and verify

Windows:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pytest
python -m ruff check .
```

macOS/Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pytest
python -m ruff check .
```

## Safe sample runs

```bash
python -m tri_arb.main --exchange binance --duration-minutes 10 --max-cycles 20 \
  --storage-mode compact --min-free-gib 5
python -m tri_arb.main --exchange mexc --duration-minutes 10 --max-cycles 20 \
  --fee-rate 0.0005 --storage-mode compact --min-free-gib 5
```

These commands use public market data and paper simulation only. Output is
written below `data/` unless `--data-dir` selects another location.

Generate a report from an existing local artifact without network access:

```bash
python -m tri_arb.main --report-only data/signals/signals_<run-id>.jsonl
```

Compare generated Markdown reports:

```bash
python -m tri_arb.tools.compare_reports data/reports/report_*.md
```

## Reproducing published conclusions

The exact raw exchange streams were intentionally not published. The aggregate
tables in `public_results/` support review of the stated conclusions, but they
cannot replay each order-book state. A fresh run is a new market observation
and should not be expected to reproduce identical symbols, counts, or edges.

## Known limits

- Public WebSocket timing is not order latency.
- Displayed depth is not a fill guarantee and queue position is unknown.
- Exchange filters and fee schedules change.
- Compact mode keeps exact cumulative counters and bounded samples/top records,
  not every raw event.
- Paper PnL omits leg risk and cannot establish deployable profitability.
