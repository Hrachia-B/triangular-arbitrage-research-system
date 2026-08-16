# Windows runbook

This runbook installs and runs the research-only MEXC Spot observer on Windows.
It reads market data, estimates execution constraints, and writes local research
artifacts. It does not place orders or establish live profitability.

## Prerequisites

1. Install 64-bit Python 3.12 (Python 3.11 is also supported).
2. Install Git for Windows.
3. For the optional fee lookup, create a dedicated MEXC API key with trading,
   transfer, and withdrawal permissions disabled. Use an IP allowlist when
   available. The project only makes a signed, read-only trade-fee request.

Never put an exchange key, exchange secret, or GitHub token in a command, clone
URL, commit, screenshot, report, or support message.

## Clone and set up

```powershell
git clone https://github.com/<owner>/triangular-arbitrage-research-system.git
Set-Location .\triangular-arbitrage-research-system
.\scripts\setup_windows.ps1
```

The setup script creates `.venv`, installs the dependencies, runs the offline
test suite, and creates `.env` through hidden prompts if it is absent. A virtual
environment copied from macOS or Linux must not be reused on Windows.

If script execution is disabled, enable it only for the current process:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\scripts\setup_windows.ps1
```

## Choose a data directory

Without configuration, run scripts write generated data under the repository's
ignored `data` directory. Long experiments are better placed on a separate
drive. Set the path once in the current PowerShell session:

```powershell
$env:TRI_ARB_DATA_DIR = "E:\tri-arb-data"
.\scripts\setup_external_data_dir.ps1 -DataDir $env:TRI_ARB_DATA_DIR
```

Use any writable absolute path with adequate free space. The same location can
also be passed directly to each script with `-DataDir`. Keep the drive connected
throughout a run.

## Required experiment sequence

Activate the environment if the setup window is no longer open:

```powershell
.\.venv\Scripts\Activate.ps1
```

1. Fetch and normalize the current MEXC fee schedule:

   ```powershell
   .\scripts\check_mexc_fees.ps1
   ```

   The raw response is stored under `$env:TRI_ARB_DATA_DIR\account` (or the
   repository `data\account` fallback). The normalized configuration is stored
   at `configs\generated\mexc_account_fee.yaml`. Both locations are ignored by
   Git. Rerun the check after cloning and whenever fees may have changed.

2. Optionally inspect the current public discovery universe:

   ```powershell
   .\scripts\run_mexc_discovery.ps1
   ```

3. Run the required ten-minute validation:

   ```powershell
   .\scripts\run_mexc_10min.ps1
   ```

4. Inspect `reports\latest.md` below the selected data directory. If the fee
   check and validation completed normally, start the 48-hour experiment:

   ```powershell
   .\scripts\run_mexc_48h.ps1
   ```

5. Start the seven-day experiment only if the completed 48-hour report says
   `48H_DECISION: CONTINUE_TO_7D`:

   ```powershell
   .\scripts\run_mexc_7d.ps1
   ```

The seven-day script checks the latest report and refuses to run without that
exact decision. A continuation decision only authorizes more observation, not
live trading.

## Long-run operation and storage

Keep the computer plugged in, the network stable, and sleep disabled for the
duration of the experiment. Restore the normal power settings afterward. If
needed, an administrator can disable only plugged-in sleep and hibernation:

```powershell
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
```

The full storage mode writes high-volume JSONL diagnostics. The supplied long-
run scripts use compact mode, retaining exact aggregate counters, bounded top
opportunities, profitable and near-break-even signals, and deterministic raw
samples. They require at least:

- 15 GiB free for the 48-hour compact run;
- 50 GiB free for the seven-day compact run.

These are safety gates, not predictions of actual consumption. Monitor free
space during long runs. Hourly checkpoints are a better progress indicator than
console activity.

## Reports and logs

Artifacts are written under the selected data directory:

```text
reports\latest.md
reports\latest_summary.csv
reports\checkpoint_<timestamp>.md
logs\mexc_48h_console.txt
logs\mexc_7d_console.txt
```

Collect the newest report artifacts in the ignored `exports` directory with:

```powershell
.\scripts\collect_reports.ps1
```

Before every commit or push, verify that credentials and generated observations
remain ignored:

```powershell
git check-ignore .env
git status --short
```

`.env` must be ignored and must never appear in `git status --short`.

## Stopping safely

Press **Ctrl+C once** in the active PowerShell window, then allow Python to stop
the WebSocket tasks and finish the report. Repeated interrupts, closing the
window, or terminating Python can leave the final report incomplete. After a
forced interruption, use the most recent checkpoint and treat the run as
incomplete.

## Interpretation

- The simulator uses public market data and optional locally supplied fee
  assumptions. It performs no live trading.
- Public order-book data cannot establish queue position or fill probability.
- Historical or simulated output does not guarantee future results.
- `CONTINUE_TO_7D` means collect more evidence. It is not permission to trade.
