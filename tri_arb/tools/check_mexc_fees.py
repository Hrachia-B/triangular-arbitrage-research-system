"""CLI for the isolated read-only MEXC Spot account-fee checker."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

from tri_arb.account_fees import (
    DEFAULT_ENV_PATH,
    DEFAULT_NORMALIZED_FEE_PATH,
    DEFAULT_RAW_FEE_DIR,
    AccountFeeError,
    MexcReadOnlyFeeClient,
    check_mexc_fees,
    combine_explicit_symbols,
    load_mexc_credentials,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tri_arb.tools.check_mexc_fees",
        description=(
            "Read MEXC Spot account trade fees using only authenticated "
            "GET /api/v3/tradeFee. This tool cannot trade or inspect balances."
        ),
    )
    parser.add_argument(
        "--symbols",
        help="optional comma-separated symbols; omit for one all-fees request",
    )
    parser.add_argument(
        "--discovery-selection",
        "--selection-json",
        dest="discovery_selection",
        type=Path,
        help="optional selected_symbols or selected_cycles JSON from discovery",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=DEFAULT_ENV_PATH,
        help="credential file used after environment variables (default: .env)",
    )
    parser.add_argument(
        "--raw-output-dir",
        type=Path,
        default=DEFAULT_RAW_FEE_DIR,
        help="raw response directory (default: data/account)",
    )
    parser.add_argument(
        "--config-output",
        type=Path,
        default=DEFAULT_NORMALIZED_FEE_PATH,
        help="safe normalized YAML path",
    )
    return parser


async def _run(args: argparse.Namespace) -> int:
    credentials = load_mexc_credentials(env_path=args.env_file)
    symbols = combine_explicit_symbols(args.symbols, args.discovery_selection)
    async with MexcReadOnlyFeeClient(credentials) as client:
        result = await check_mexc_fees(
            credentials,
            symbols=symbols,
            client=client,
            raw_output_dir=args.raw_output_dir,
            config_output_path=args.config_output,
        )

    print(f"Symbols checked: {len(result.schedule.symbol_fees)}")
    for symbol, fee in sorted(result.schedule.symbol_fees.items()):
        print(f"{symbol}: maker fee={fee.maker_fee}, taker fee={fee.taker_fee}")
    print(f"Maximum taker fee: {result.schedule.maximum_taker_fee}")
    print(
        f"Recommended conservative simulation fee: {result.schedule.recommended_conservative_fee}"
    )
    print(f"Raw fee response: {result.raw_path}")
    print(f"Normalized fee config: {result.config_path}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except AccountFeeError as exc:
        print(f"Fee check failed: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Fee check interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
