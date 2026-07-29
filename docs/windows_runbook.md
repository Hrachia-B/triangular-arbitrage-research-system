# Windows runbook

This runbook transfers and runs the research-only MEXC Spot observer on a
Windows laptop. It does not place orders and it cannot prove live
profitability.

## Before cloning

1. Install 64-bit Python 3.12 from Python.org. Python 3.11 is also supported.
   Enable the Python launcher during installation.
2. Install Git for Windows. Keep Git Credential Manager enabled so credentials
   do not need to be embedded in a clone URL.
3. In MEXC, create a dedicated API key for the fee check. Disable trading,
   transfers, and withdrawals, and use an IP allowlist if MEXC offers one for
   the key. This project only uses the signed read-only
   `GET /api/v3/tradeFee` request.
4. Confirm that the GitHub repository is private before cloning it.

Never put a GitHub token, MEXC key, or MEXC secret in a command, clone URL,
commit, screenshot, report, or support message.

## Clone and set up

Open PowerShell and run:

```powershell
git clone https://github.com/Hrachia-B/triangular_arbitrage_system.git
Set-Location .\triangular_arbitrage_system
.\scripts\setup_windows.ps1
```

The setup script:

- selects Python 3.12 or 3.11;
- creates and activates `.venv`;
- upgrades pip and installs `requirements.txt`;
- creates the local data directories;
- runs the offline test suite;
- securely prompts for the MEXC API key and secret when `.env` is missing;
- hides prompt input and never prints either value.

If PowerShell reports that script execution is disabled, enable scripts only
for the current PowerShell process, then retry:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\scripts\setup_windows.ps1
```

Do not set a permanent machine-wide execution-policy bypass.

The Mac `.venv` must never be copied to Windows. The setup script creates a
new Windows environment and every run script explicitly uses
`.venv\Scripts\python.exe`.

## Required experiment sequence

1. Fetch and normalize the account's current MEXC fee information:

   ```powershell
   .\scripts\check_mexc_fees.ps1
   ```

   The raw response is written under `data\account\`, and the normalized local
   configuration is written to
   `configs\generated\mexc_account_fee.yaml`. Both locations are ignored by
   Git. The tool must be rerun on a new clone and whenever the account's fees
   may have changed.

   If MEXC rejects the default one-call request because `symbol` is required,
   create a public discovery selection and check only that bounded universe:

   ```powershell
   .\scripts\run_mexc_discovery.ps1
   $selection = Get-ChildItem .\data\raw\selected_symbols_*.json |
       Sort-Object LastWriteTimeUtc -Descending |
       Select-Object -First 1 -ExpandProperty FullName
   .\scripts\check_mexc_fees.ps1 --discovery-selection $selection
   ```

   Explicit symbol checks are sequential and paced. Do not replace this with a
   parallel request fan-out.

2. Optionally verify the current public discovery universe:

   ```powershell
   .\scripts\run_mexc_discovery.ps1
   ```

3. Run the required 10-minute validation:

   ```powershell
   .\scripts\run_mexc_10min.ps1
   ```

4. Inspect `data\reports\latest.md`. If the fee check and 10-minute run
   completed normally, start the 48-hour experiment:

   ```powershell
   .\scripts\run_mexc_48h.ps1
   ```

5. Only if the completed 48-hour report explicitly says
   `48H_DECISION: CONTINUE_TO_7D`, start the 7-day experiment:

   ```powershell
   .\scripts\run_mexc_7d.ps1
   ```

The 7-day script also reads `data\reports\latest.md` and refuses to start
unless that decision is present.

## Power, storage, and long-run operation

Keep the laptop plugged in, maintain a stable network connection, and do not
close the PowerShell window. In Windows Settings, open **System > Power &
battery > Screen and sleep** and set sleep while plugged in to **Never** for
the experiment. Restore the normal setting afterward.

An administrator can alternatively disable only the plugged-in sleep and
hibernate timers:

```powershell
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
```

The observer writes high-volume JSONL diagnostics. Measurements from the Mac
produced about 1.6 GiB in a roughly 54-minute, 20-cycle run; actual Windows
volume can be higher or lower. The scripts therefore refuse to start with less
than:

- 100 GiB free for the 48-hour run;
- 500 GiB free for the 7-day run.

Those are minimum gates, not guarantees. Check free disk space throughout a
long experiment. Do not start if the laptop cannot retain the generated data.

The Python process may be quiet between status messages. A quiet console does
not mean it exited. Hourly checkpoints are the reliable progress indicator.

## Reports, checkpoints, and logs

Current artifacts are located at:

```text
data\reports\latest.md
data\reports\latest_summary.csv
data\reports\checkpoint_<timestamp>.md
```

Long-run console copies are located at:

```text
data\logs\mexc_48h_console.txt
data\logs\mexc_7d_console.txt
```

Copy only the latest report artifacts into a local export directory with:

```powershell
.\scripts\collect_reports.ps1
```

The export is written to `data\reports\export\`. Generated reports, account
fee responses, raw observations, and logs remain ignored by Git.

Before any commit or push, verify that local credentials and data remain
ignored:

```powershell
git check-ignore .env
git status --short
```

`.env` must appear as ignored and must never appear in `git status --short`.

## Stopping safely

Press **Ctrl+C once** in the active PowerShell window. Allow Python time to stop
the public WebSocket tasks and finish the report. Do not close the window,
terminate Python in Task Manager, power off the laptop, or press Ctrl+C
repeatedly while finalization is running.

If the process is forcibly interrupted, use the most recent hourly checkpoint;
the final timestamped report may be incomplete. The run scripts propagate the
Python exit code, so a nonzero exit code must be investigated before trusting
the output.

## Security and interpretation warnings

- The simulator uses public market data and account-derived fee assumptions. It
  performs no live trading.
- The fee checker is isolated to the read-only MEXC trade-fee endpoint. Never
  enable trading or withdrawal permissions for its key.
- A public-data simulation cannot establish fill probability or prove live
  profitability.
- `CONTINUE_TO_7D` means collect more evidence. It is not permission to trade.
- Keep the repository private even though secrets and generated account data
  are excluded.
