# Security boundaries

## Credentials

The simulator uses public market data. No credential is required for normal
Binance or MEXC observation.

The optional MEXC fee checker reads `MEXC_API_KEY` and `MEXC_API_SECRET` from
the process environment or a local `.env`. Use a dedicated read-only key with
trading, transfers, and withdrawals disabled. Never commit `.env`, paste its
contents into an issue, or include it in a report bundle.

`.env.example` contains placeholders only. Generated normalized fee
configuration and authenticated raw responses are ignored and are not part of
the public results.

## Network boundary

The observer uses allow-listed public market-data REST paths and public
WebSocket streams. The optional authenticated client permits only the signed
read-only MEXC `GET /api/v3/tradeFee` endpoint.

There is no order placement, cancellation, balance management, transfer,
withdrawal, or private execution path. Future execution work should remain in a
separate repository and undergo an independent security review.

## Publication checklist

Safe public files include source code, synthetic tests, checked-in YAML
profiles, documentation, and sanitized aggregate reports.

Never publish:

- `.env` or credential-manager exports;
- authenticated raw responses or generated account fee configuration;
- raw JSONL streams, snapshots, and operational logs;
- local usernames, home directories, or device-specific paths;
- screenshots or command output containing environment values;
- private keys, tokens, signatures, cookies, or account identifiers.

Run the tracked-file search described in the root README before every public
release. Matches in placeholder names, redaction code, and synthetic tests must
still be reviewed rather than ignored automatically.
