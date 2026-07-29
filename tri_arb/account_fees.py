"""Isolated, read-only MEXC Spot account-fee support.

This module has one deliberately narrow private-API capability: authenticated
``GET /api/v3/tradeFee`` requests.  It cannot place or cancel orders, inspect
balances, or call any other private endpoint.  The returned fees are account
metadata used by the paper simulator; no execution functionality lives here.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
import os
import time
from collections.abc import Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final
from urllib.parse import urlencode

try:
    import aiohttp
except ImportError:  # pragma: no cover - the project runtime installs aiohttp.
    aiohttp = None  # type: ignore[assignment]

try:
    import yaml
except ImportError:  # pragma: no cover - the project runtime installs PyYAML.
    yaml = None  # type: ignore[assignment]


MEXC_API_KEY_ENV: Final = "MEXC_API_KEY"
MEXC_API_SECRET_ENV: Final = "MEXC_API_SECRET"
MEXC_REST_BASE_URL: Final = "https://api.mexc.com"
MEXC_TRADE_FEE_PATH: Final = "/api/v3/tradeFee"
MEXC_TRADE_FEE_METHOD: Final = "GET"
MEXC_API_KEY_HEADER: Final = "X-MEXC-APIKEY"
FEE_SOURCE: Final = "mexc_account_tradeFee_read_only"
DEFAULT_ENV_PATH: Final = Path(".env")
DEFAULT_RAW_FEE_DIR: Final = Path("data/account")
DEFAULT_NORMALIZED_FEE_PATH: Final = Path("configs/generated/mexc_account_fee.yaml")
MAX_EXPLICIT_SYMBOLS: Final = 100
DEFAULT_EXPLICIT_REQUEST_INTERVAL_SECONDS: Final = 0.6
DEFAULT_RATE_LIMIT_RETRIES: Final = 2
DEFAULT_RETRY_BASE_DELAY_SECONDS: Final = 0.5
MAX_RETRY_DELAY_SECONDS: Final = 60.0

_FEE_CONTAINER_KEYS = (
    "data",
    "result",
    "fee",
    "fees",
    "tradeFee",
    "tradeFees",
    "commission",
    "commissions",
    "rows",
    "list",
)
_MAKER_KEYS = (
    "makerCommission",
    "makerFeeRate",
    "makerFee",
    "maker_rate",
    "maker_fee",
    "maker",
)
_TAKER_KEYS = (
    "takerCommission",
    "takerFeeRate",
    "takerFee",
    "taker_rate",
    "taker_fee",
    "taker",
)
_SYMBOL_KEYS = ("symbol", "market", "marketSymbol", "s")
_RETRYABLE_FEE_STATUSES = frozenset({418, 429, 500, 502, 503, 504})


class AccountFeeError(RuntimeError):
    """Stable integration error for loading or checking account fee metadata."""


class FeeError(AccountFeeError):
    """Base class for safe, user-facing fee-checker failures."""


class FeeConfigurationError(FeeError):
    """Credentials, selection input, or normalized fee configuration is invalid."""


class FeeSafetyError(FeeError):
    """A request attempted to cross the hard read-only endpoint boundary."""


class MexcFeeAPIError(FeeError):
    """The single allowed MEXC fee request failed."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True, slots=True, repr=False)
class MexcCredentials:
    """MEXC credentials whose representation never discloses either value."""

    api_key: str
    api_secret: str

    def __post_init__(self) -> None:
        if not isinstance(self.api_key, str) or not isinstance(self.api_secret, str):
            raise FeeConfigurationError("MEXC API credentials must be strings")
        api_key = self.api_key.strip()
        api_secret = self.api_secret.strip()
        if not api_key:
            raise FeeConfigurationError(f"{MEXC_API_KEY_ENV} is empty")
        if not api_secret:
            raise FeeConfigurationError(f"{MEXC_API_SECRET_ENV} is empty")
        object.__setattr__(self, "api_key", api_key)
        object.__setattr__(self, "api_secret", api_secret)

    def __repr__(self) -> str:
        return "MexcCredentials(api_key=<redacted>, api_secret=<redacted>)"


@dataclass(frozen=True, slots=True)
class SymbolFee:
    """Normalized decimal maker/taker fees for one MEXC Spot symbol."""

    symbol: str
    maker_fee: Decimal
    taker_fee: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        object.__setattr__(
            self,
            "maker_fee",
            _fee_decimal(self.maker_fee, "maker fee", allow_negative=True),
        )
        object.__setattr__(self, "taker_fee", _fee_decimal(self.taker_fee, "taker fee"))


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    """Reusable symbol-fee schedule with a conservative maximum-fee fallback."""

    source: str
    fallback_taker_fee: Decimal
    symbol_taker_fees: Mapping[str, Decimal]
    generated_at: str | None = None
    symbol_maker_fees: Mapping[str, Decimal] = field(default_factory=dict)

    def __post_init__(self) -> None:
        normalized_taker: dict[str, Decimal] = {}
        for symbol, value in self.symbol_taker_fees.items():
            normalized_symbol = normalize_symbol(symbol)
            normalized_value = _fee_decimal(
                value,
                f"taker fee for {normalized_symbol}",
            )
            normalized_taker[normalized_symbol] = max(
                normalized_taker.get(normalized_symbol, Decimal("0")),
                normalized_value,
            )
        normalized_maker: dict[str, Decimal] = {}
        for symbol, value in self.symbol_maker_fees.items():
            normalized_symbol = normalize_symbol(symbol)
            normalized_value = _fee_decimal(
                value,
                f"maker fee for {normalized_symbol}",
                allow_negative=True,
            )
            previous_maker = normalized_maker.get(normalized_symbol)
            normalized_maker[normalized_symbol] = (
                normalized_value
                if previous_maker is None
                else max(previous_maker, normalized_value)
            )

        configured_maximum = _fee_decimal(
            self.fallback_taker_fee,
            "fallback taker fee",
        )
        observed_maximum = max(
            normalized_taker.values(),
            default=Decimal("0"),
        )
        conservative_maximum = max(configured_maximum, observed_maximum)
        object.__setattr__(self, "source", str(self.source or FEE_SOURCE).strip() or FEE_SOURCE)
        object.__setattr__(self, "fallback_taker_fee", conservative_maximum)
        object.__setattr__(
            self,
            "symbol_taker_fees",
            MappingProxyType(normalized_taker),
        )
        object.__setattr__(
            self,
            "symbol_maker_fees",
            MappingProxyType(normalized_maker),
        )

    @classmethod
    def from_fees(
        cls,
        fees: Iterable[SymbolFee],
        *,
        source: str = FEE_SOURCE,
        generated_at: str | None = None,
    ) -> FeeSchedule:
        """Build a schedule, conservatively merging duplicate symbol records."""

        merged: dict[str, SymbolFee] = {}
        for raw_fee in fees:
            fee = raw_fee if isinstance(raw_fee, SymbolFee) else SymbolFee(**raw_fee)
            previous = merged.get(fee.symbol)
            if previous is None:
                merged[fee.symbol] = fee
            else:
                merged[fee.symbol] = SymbolFee(
                    symbol=fee.symbol,
                    maker_fee=max(previous.maker_fee, fee.maker_fee),
                    taker_fee=max(previous.taker_fee, fee.taker_fee),
                )
        if not merged:
            raise FeeConfigurationError("MEXC trade-fee response contained no usable symbol fees")
        maximum = max(fee.taker_fee for fee in merged.values())
        return cls(
            source=source,
            fallback_taker_fee=maximum,
            symbol_taker_fees={symbol: fee.taker_fee for symbol, fee in merged.items()},
            generated_at=generated_at,
            symbol_maker_fees={symbol: fee.maker_fee for symbol, fee in merged.items()},
        )

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> FeeSchedule:
        """Load the safe YAML mapping emitted by :func:`save_fee_schedule`."""

        if not isinstance(payload, Mapping):
            raise FeeConfigurationError("normalized MEXC fee config must be a mapping")
        fees: dict[str, SymbolFee] = {}

        def merge_fee(symbol: str, maker: Any, taker: Any) -> None:
            candidate = SymbolFee(symbol, maker, taker)
            previous = fees.get(candidate.symbol)
            if previous is None:
                fees[candidate.symbol] = candidate
                return
            fees[candidate.symbol] = SymbolFee(
                candidate.symbol,
                max(previous.maker_fee, candidate.maker_fee),
                max(previous.taker_fee, candidate.taker_fee),
            )

        direct_takers = payload.get("symbol_taker_fees")
        direct_makers = payload.get("symbol_maker_fees", {})
        if direct_takers is not None:
            if not isinstance(direct_takers, Mapping) or not isinstance(direct_makers, Mapping):
                raise FeeConfigurationError(
                    "symbol_taker_fees and symbol_maker_fees must be mappings"
                )
            normalized_direct_makers = {
                normalize_symbol(str(raw_symbol)): maker
                for raw_symbol, maker in direct_makers.items()
            }
            for raw_symbol, taker in direct_takers.items():
                symbol = normalize_symbol(str(raw_symbol))
                merge_fee(
                    symbol,
                    normalized_direct_makers.get(symbol, Decimal("0")),
                    taker,
                )

        raw_symbols = payload.get("symbols", payload.get("symbol_fees"))
        if raw_symbols is not None:
            if not isinstance(raw_symbols, Mapping):
                raise FeeConfigurationError("normalized MEXC fee config symbols must be a mapping")
            for raw_symbol, raw_values in raw_symbols.items():
                symbol = normalize_symbol(str(raw_symbol))
                if not isinstance(raw_values, Mapping):
                    raise FeeConfigurationError(f"fee config entry for {symbol} must be a mapping")
                maker = _first_value(raw_values, ("maker_fee", *_MAKER_KEYS))
                taker = _first_value(raw_values, ("taker_fee", *_TAKER_KEYS))
                if taker is None:
                    raise FeeConfigurationError(f"fee config entry for {symbol} requires taker_fee")
                merge_fee(
                    symbol,
                    Decimal("0") if maker is None else maker,
                    taker,
                )

        maximum_candidates = [
            _fee_decimal(payload[key], key.replace("_", " "))
            for key in (
                "fallback_taker_fee",
                "maximum_taker_fee",
                "recommended_conservative_fee",
            )
            if payload.get(key) is not None
        ]
        if not maximum_candidates:
            if not fees:
                raise FeeConfigurationError(
                    "normalized MEXC fee config has no symbol fees or fallback_taker_fee"
                )
            maximum_candidates.append(max(fee.taker_fee for fee in fees.values()))
        return cls(
            source=str(payload.get("fee_source", payload.get("source", FEE_SOURCE))),
            fallback_taker_fee=max(maximum_candidates),
            symbol_taker_fees={symbol: fee.taker_fee for symbol, fee in fees.items()},
            generated_at=(
                str(payload["generated_at"]) if payload.get("generated_at") is not None else None
            ),
            symbol_maker_fees={symbol: fee.maker_fee for symbol, fee in fees.items()},
        )

    @classmethod
    def load(cls, path: str | Path = DEFAULT_NORMALIZED_FEE_PATH) -> FeeSchedule:
        return load_fee_schedule(path)

    @property
    def recommended_conservative_fee(self) -> Decimal:
        return self.fallback_taker_fee

    @property
    def maximum_taker_fee(self) -> Decimal:
        """Compatibility name used by the standalone checker output."""

        return self.fallback_taker_fee

    @property
    def taker_fees(self) -> Mapping[str, Decimal]:
        return self.symbol_taker_fees

    @property
    def symbol_fees(self) -> Mapping[str, SymbolFee]:
        """Compatibility view combining the separate maker/taker mappings."""

        return MappingProxyType(
            {
                symbol: SymbolFee(
                    symbol,
                    self.symbol_maker_fees.get(symbol, Decimal("0")),
                    taker_fee,
                )
                for symbol, taker_fee in self.symbol_taker_fees.items()
            }
        )

    def fee_for_symbol(self, symbol: str) -> Decimal:
        """Return a symbol fee or the schedule maximum as a safe fallback."""

        return self.symbol_taker_fees.get(
            normalize_symbol(symbol),
            self.fallback_taker_fee,
        )

    taker_fee_for = fee_for_symbol
    get_taker_fee = taker_fee_for

    def to_assumptions(self) -> dict[str, Any]:
        """Return report metadata containing fees only, never credentials."""

        return {
            "source": self.source,
            "fee_source": self.source,
            "fallback_taker_fee": str(self.fallback_taker_fee),
            "symbol_taker_fees": {
                symbol: str(fee) for symbol, fee in sorted(self.symbol_taker_fees.items())
            },
            "symbol_maker_fees": {
                symbol: str(fee) for symbol, fee in sorted(self.symbol_maker_fees.items())
            },
            "generated_at": self.generated_at,
        }


@dataclass(frozen=True, slots=True, repr=False)
class FeeCheckResult:
    """Artifacts and normalized schedule produced by one read-only check."""

    schedule: FeeSchedule
    raw_response: Any
    raw_path: Path
    config_path: Path
    requested_symbols: tuple[str, ...]

    def __repr__(self) -> str:
        return (
            "FeeCheckResult("
            f"schedule={self.schedule!r}, "
            f"raw_path={self.raw_path!r}, "
            f"config_path={self.config_path!r}, "
            f"requested_symbols={self.requested_symbols!r}, "
            "raw_response=<redacted-response>)"
        )


def normalize_symbol(symbol: str) -> str:
    normalized = str(symbol).strip().upper()
    if not normalized or not normalized.isalnum():
        raise FeeConfigurationError("MEXC symbol must be a non-empty alphanumeric value")
    return normalized


def parse_symbols(value: str | Iterable[str] | None) -> tuple[str, ...]:
    """Normalize a comma-separated value or symbol iterable without duplicates."""

    if value is None:
        return ()
    raw_values: Iterable[Any] = value.split(",") if isinstance(value, str) else value
    symbols: list[str] = []
    for raw in raw_values:
        if raw is None or not str(raw).strip():
            continue
        symbol = normalize_symbol(str(raw))
        if symbol not in symbols:
            symbols.append(symbol)
    return tuple(symbols)


def load_discovery_symbols(path: str | Path) -> tuple[str, ...]:
    """Extract symbols from recorder symbol/cycle selection JSON."""

    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise FeeConfigurationError(f"discovery selection JSON not found: {source}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise FeeConfigurationError(f"could not read discovery selection JSON: {source}") from exc

    symbols: list[str] = []

    def add(raw_symbol: Any) -> None:
        if raw_symbol is None or not str(raw_symbol).strip():
            return
        try:
            symbol = normalize_symbol(str(raw_symbol))
        except FeeConfigurationError:
            return
        if symbol not in symbols:
            symbols.append(symbol)

    def visit(value: Any, *, strings_are_symbols: bool = False) -> None:
        if isinstance(value, str):
            if strings_are_symbols:
                add(value)
            return
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            for item in value:
                visit(item, strings_are_symbols=strings_are_symbols)
            return
        if not isinstance(value, Mapping):
            return
        for key in _SYMBOL_KEYS:
            if key in value:
                add(value[key])
        raw_symbols = value.get("symbols")
        if isinstance(raw_symbols, Sequence) and not isinstance(
            raw_symbols, (str, bytes, bytearray)
        ):
            visit(raw_symbols, strings_are_symbols=True)
        legs = value.get("legs")
        if isinstance(legs, Sequence) and not isinstance(legs, (str, bytes, bytearray)):
            visit(legs)
        records = value.get("records")
        if isinstance(records, Sequence) and not isinstance(records, (str, bytes, bytearray)):
            visit(records, strings_are_symbols=True)
        data = value.get("data")
        if isinstance(data, (Mapping, list, tuple)):
            visit(data, strings_are_symbols=strings_are_symbols)

    visit(payload, strings_are_symbols=isinstance(payload, Sequence))
    if not symbols:
        raise FeeConfigurationError(
            f"discovery selection JSON contains no recognizable symbols: {source}"
        )
    return tuple(symbols)


def combine_explicit_symbols(
    symbols: str | Iterable[str] | None = None,
    discovery_selection: str | Path | None = None,
) -> tuple[str, ...]:
    selected = list(parse_symbols(symbols))
    if discovery_selection is not None:
        for symbol in load_discovery_symbols(discovery_selection):
            if symbol not in selected:
                selected.append(symbol)
    if len(selected) > MAX_EXPLICIT_SYMBOLS:
        raise FeeConfigurationError(
            f"explicit fee selection is limited to {MAX_EXPLICIT_SYMBOLS} symbols; "
            "omit symbol selection to make one all-fees request"
        )
    return tuple(selected)


def load_mexc_credentials(
    *,
    environ: Mapping[str, str] | None = None,
    env_path: str | Path = DEFAULT_ENV_PATH,
) -> MexcCredentials:
    """Load credentials with process environment taking precedence over ``.env``."""

    environment = os.environ if environ is None else environ
    environment_key = str(environment.get(MEXC_API_KEY_ENV) or "").strip()
    environment_secret = str(environment.get(MEXC_API_SECRET_ENV) or "").strip()
    dotenv = {} if environment_key and environment_secret else _read_dotenv(Path(env_path))
    api_key = str(environment_key or dotenv.get(MEXC_API_KEY_ENV) or "").strip()
    api_secret = str(environment_secret or dotenv.get(MEXC_API_SECRET_ENV) or "").strip()
    missing = [
        name
        for name, value in (
            (MEXC_API_KEY_ENV, api_key),
            (MEXC_API_SECRET_ENV, api_secret),
        )
        if not value
    ]
    if missing:
        names = " and ".join(missing)
        raise FeeConfigurationError(
            f"Missing {names}. Set them in the environment or in {Path(env_path)}."
        )
    return MexcCredentials(api_key=api_key, api_secret=api_secret)


def deterministic_query_string(params: Mapping[str, Any] | Iterable[tuple[str, Any]]) -> str:
    """Return the exact sorted query string that will be signed and transmitted."""

    items = list(params.items()) if isinstance(params, Mapping) else list(params)
    normalized: list[tuple[str, str]] = []
    for raw_key, raw_value in items:
        key = str(raw_key)
        if key == "signature":
            raise FeeSafetyError("signature must not be supplied as an input parameter")
        if raw_value is None:
            continue
        if isinstance(raw_value, bool):
            value = "true" if raw_value else "false"
        else:
            value = str(raw_value)
        normalized.append((key, value))
    normalized.sort(key=lambda item: (item[0], item[1]))
    return urlencode(normalized)


def sign_query_string(query_string: str, api_secret: str) -> str:
    """Return lowercase HMAC-SHA256 for an already deterministic query string."""

    if not api_secret:
        raise FeeConfigurationError(f"{MEXC_API_SECRET_ENV} is empty")
    return hmac.new(
        api_secret.encode("utf-8"),
        query_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def build_signed_query(
    params: Mapping[str, Any] | Iterable[tuple[str, Any]],
    api_secret: str,
) -> str:
    unsigned = deterministic_query_string(params)
    signature = sign_query_string(unsigned, api_secret)
    separator = "&" if unsigned else ""
    return f"{unsigned}{separator}signature={signature}"


def validate_read_only_fee_request(method: str, path: str) -> None:
    """Enforce the only authenticated method/path this subsystem can call."""

    if method != MEXC_TRADE_FEE_METHOD or path != MEXC_TRADE_FEE_PATH:
        raise FeeSafetyError("MEXC account-fee subsystem permits only HTTPS GET /api/v3/tradeFee")


class MexcReadOnlyFeeClient:
    """Authenticated client physically restricted to the MEXC trade-fee GET."""

    def __init__(
        self,
        credentials: MexcCredentials,
        *,
        session: Any | None = None,
        request_timeout_seconds: float = 10.0,
        timestamp_provider: Any | None = None,
    ) -> None:
        if not isinstance(credentials, MexcCredentials):
            raise TypeError("credentials must be MexcCredentials")
        if request_timeout_seconds <= 0:
            raise FeeConfigurationError("request timeout must be positive")
        self._credentials = credentials
        self._session = session
        self._owns_session = session is None
        self._request_timeout_seconds = float(request_timeout_seconds)
        self._timestamp_provider = timestamp_provider or (lambda: time.time_ns() // 1_000_000)
        self._closed = False

    def __repr__(self) -> str:
        return "MexcReadOnlyFeeClient(endpoint=<read-only-trade-fee>)"

    async def __aenter__(self) -> MexcReadOnlyFeeClient:
        await self._ensure_session()
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_session and self._session is not None:
            await self._session.close()

    async def get_trade_fees(self, symbol: str | None = None) -> Any:
        """Fetch one symbol or, when omitted, all account Spot fee rows once."""

        try:
            timestamp = int(self._timestamp_provider())
        except (TypeError, ValueError, OverflowError):
            raise FeeConfigurationError("MEXC request timestamp must be an integer") from None
        if timestamp <= 0:
            raise FeeConfigurationError("MEXC request timestamp must be positive")
        params: dict[str, Any] = {"timestamp": timestamp}
        requested_symbol = normalize_symbol(symbol) if symbol is not None else None
        if requested_symbol is not None:
            params["symbol"] = requested_symbol
        return await self._request_json(MEXC_TRADE_FEE_PATH, params)

    async def _ensure_session(self) -> Any:
        if self._closed:
            raise FeeConfigurationError("MEXC fee client is closed")
        if self._session is None:
            if aiohttp is None:
                raise FeeConfigurationError("aiohttp is required for the MEXC fee checker")
            timeout = aiohttp.ClientTimeout(total=self._request_timeout_seconds)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def _request_json(self, path: str, params: Mapping[str, Any]) -> Any:
        validate_read_only_fee_request(MEXC_TRADE_FEE_METHOD, path)
        session = await self._ensure_session()
        signed_query = build_signed_query(params, self._credentials.api_secret)
        endpoint = f"{MEXC_REST_BASE_URL}{MEXC_TRADE_FEE_PATH}?{signed_query}"
        headers = {MEXC_API_KEY_HEADER: self._credentials.api_key}
        try:
            async with session.get(endpoint, headers=headers) as response:
                if response.status != 200:
                    raise MexcFeeAPIError(
                        f"MEXC trade-fee GET failed with HTTP status {response.status}",
                        status=int(response.status),
                        retry_after_seconds=_retry_after_seconds(
                            getattr(response, "headers", {}).get("Retry-After")
                        ),
                    )
                try:
                    payload = await response.json(content_type=None)
                except (json.JSONDecodeError, TypeError, ValueError):
                    raise MexcFeeAPIError("MEXC trade-fee GET returned invalid JSON") from None
        except MexcFeeAPIError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise MexcFeeAPIError(f"MEXC trade-fee GET failed: {type(exc).__name__}") from None
        if not isinstance(payload, (Mapping, list)):
            raise MexcFeeAPIError("MEXC trade-fee GET returned an unexpected response shape")
        return payload


def normalize_mexc_fee_response(
    payload: Any,
    *,
    requested_symbol: str | None = None,
) -> tuple[SymbolFee, ...]:
    """Normalize MEXC's single-row, list, wrapped, or symbol-keyed responses."""

    requested = normalize_symbol(requested_symbol) if requested_symbol is not None else None
    candidate_rows = _fee_rows(payload)
    if not candidate_rows:
        raise FeeConfigurationError("MEXC trade-fee response contained no fee rows")

    normalized: list[SymbolFee] = []
    for row in candidate_rows:
        symbol_value = _first_value(row, _SYMBOL_KEYS)
        symbol = normalize_symbol(symbol_value) if symbol_value not in (None, "") else requested
        if symbol is None:
            raise FeeConfigurationError("MEXC trade-fee row is missing its symbol")
        maker = _first_value(row, _MAKER_KEYS)
        taker = _first_value(row, _TAKER_KEYS)
        if maker is None or taker is None:
            continue
        normalized.append(SymbolFee(symbol, maker, taker))
    if not normalized:
        raise FeeConfigurationError("MEXC trade-fee response contained no usable maker/taker fees")
    return tuple(FeeSchedule.from_fees(normalized).symbol_fees.values())


async def _fetch_explicit_fee_with_retry(
    client: MexcReadOnlyFeeClient,
    symbol: str,
    *,
    retries: int,
    retry_base_delay_seconds: float,
    sleep: Any,
) -> Any:
    for attempt in range(retries + 1):
        try:
            return await client.get_trade_fees(symbol)
        except MexcFeeAPIError as exc:
            if exc.status not in _RETRYABLE_FEE_STATUSES or attempt >= retries:
                raise
            if (
                exc.retry_after_seconds is not None
                and exc.retry_after_seconds > MAX_RETRY_DELAY_SECONDS
            ):
                raise MexcFeeAPIError(
                    "MEXC rate limit requires a long backoff; aborting without another request",
                    status=exc.status,
                    retry_after_seconds=exc.retry_after_seconds,
                ) from None
            calculated_delay = retry_base_delay_seconds * (2**attempt)
            delay = (
                exc.retry_after_seconds if exc.retry_after_seconds is not None else calculated_delay
            )
            await _sleep_for(
                sleep,
                min(MAX_RETRY_DELAY_SECONDS, max(0.0, delay)),
            )


async def _sleep_for(sleep: Any, delay_seconds: float) -> None:
    sleep_result = sleep(delay_seconds)
    if inspect.isawaitable(sleep_result):
        await sleep_result


async def check_mexc_fees(
    credentials: MexcCredentials,
    *,
    symbols: Iterable[str] = (),
    client: MexcReadOnlyFeeClient | None = None,
    raw_output_dir: str | Path = DEFAULT_RAW_FEE_DIR,
    config_output_path: str | Path = DEFAULT_NORMALIZED_FEE_PATH,
    now: datetime | None = None,
    explicit_request_interval_seconds: float = DEFAULT_EXPLICIT_REQUEST_INTERVAL_SECONDS,
    rate_limit_retries: int = DEFAULT_RATE_LIMIT_RETRIES,
    retry_base_delay_seconds: float = DEFAULT_RETRY_BASE_DELAY_SECONDS,
    sleep: Any = asyncio.sleep,
) -> FeeCheckResult:
    """Fetch, normalize, and save account fee data without exposing credentials."""

    selected = parse_symbols(symbols)
    if len(selected) > MAX_EXPLICIT_SYMBOLS:
        raise FeeConfigurationError(
            f"explicit fee selection is limited to {MAX_EXPLICIT_SYMBOLS} symbols"
        )
    if explicit_request_interval_seconds < 0:
        raise FeeConfigurationError("explicit request interval cannot be negative")
    if rate_limit_retries < 0:
        raise FeeConfigurationError("rate-limit retries cannot be negative")
    if retry_base_delay_seconds < 0:
        raise FeeConfigurationError("retry base delay cannot be negative")
    if not callable(sleep):
        raise FeeConfigurationError("sleep must be callable")
    checked_at = _as_utc(now or datetime.now(UTC))
    owns_client = client is None
    active_client = client or MexcReadOnlyFeeClient(credentials)
    if active_client._credentials != credentials:
        raise FeeConfigurationError(
            "injected MEXC fee client credentials do not match the supplied credentials"
        )
    sensitive_values = (
        credentials.api_key,
        credentials.api_secret,
        active_client._credentials.api_key,
        active_client._credentials.api_secret,
    )
    try:
        if not selected:
            try:
                raw_response = await active_client.get_trade_fees()
                normalized = normalize_mexc_fee_response(raw_response)
            except MexcFeeAPIError as exc:
                if exc.status not in {400, 404}:
                    raise
                raise FeeConfigurationError(
                    "MEXC rejected the all-fees request; the current Spot V3 "
                    "documentation requires a symbol. Rerun with --symbols or "
                    "--discovery-selection."
                ) from None
            except FeeConfigurationError as exc:
                raise FeeConfigurationError(
                    f"{exc}. Rerun with --symbols or --discovery-selection; "
                    "the current MEXC Spot V3 documentation requires a symbol."
                ) from None
        else:
            responses: dict[str, Any] = {}
            normalized_rows: list[SymbolFee] = []
            for index, symbol in enumerate(selected):
                if index and explicit_request_interval_seconds:
                    await _sleep_for(sleep, explicit_request_interval_seconds)
                response = await _fetch_explicit_fee_with_retry(
                    active_client,
                    symbol,
                    retries=rate_limit_retries,
                    retry_base_delay_seconds=retry_base_delay_seconds,
                    sleep=sleep,
                )
                responses[symbol] = response
                normalized_rows.extend(
                    normalize_mexc_fee_response(response, requested_symbol=symbol)
                )
            raw_response = responses
            normalized = tuple(normalized_rows)
        schedule = FeeSchedule.from_fees(
            normalized,
            generated_at=checked_at.isoformat(),
        )
        safe_response = _redact(
            raw_response,
            tuple(dict.fromkeys(value for value in sensitive_values if value)),
        )
        raw_path = save_raw_fee_response(
            safe_response,
            output_dir=raw_output_dir,
            timestamp=checked_at,
            sensitive_values=sensitive_values,
        )
        config_path = save_fee_schedule(
            schedule,
            path=config_output_path,
        )
        return FeeCheckResult(
            schedule=schedule,
            raw_response=safe_response,
            raw_path=raw_path,
            config_path=config_path,
            requested_symbols=selected,
        )
    finally:
        if owns_client:
            await active_client.close()


def save_raw_fee_response(
    payload: Any,
    *,
    output_dir: str | Path = DEFAULT_RAW_FEE_DIR,
    timestamp: datetime | None = None,
    sensitive_values: Iterable[str] = (),
) -> Path:
    """Persist the API response after defensively redacting credential values."""

    moment = _as_utc(timestamp or datetime.now(UTC))
    target_dir = Path(output_dir)
    target = target_dir / f"mexc_fees_{moment.strftime('%Y%m%dT%H%M%SZ')}.json"
    safe_payload = _redact(payload, tuple(value for value in sensitive_values if value))
    try:
        rendered = json.dumps(safe_payload, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
        target_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write(target, rendered)
    except (OSError, TypeError, ValueError) as exc:
        raise FeeConfigurationError(f"could not write raw MEXC fee response: {target}") from exc
    _restrict_file_permissions(target)
    return target


def save_fee_schedule(
    schedule: FeeSchedule,
    *,
    path: str | Path = DEFAULT_NORMALIZED_FEE_PATH,
) -> Path:
    """Write secret-free normalized YAML for later simulation use."""

    if yaml is None:
        raise FeeConfigurationError("PyYAML is required to write the MEXC fee config")
    target = Path(path)
    payload = {
        "exchange": "mexc",
        "fee_source": schedule.source,
        "generated_at": schedule.generated_at,
        "fallback_taker_fee": str(schedule.fallback_taker_fee),
        "maximum_taker_fee": str(schedule.fallback_taker_fee),
        "recommended_conservative_fee": str(schedule.recommended_conservative_fee),
        "symbol_taker_fees": {
            symbol: str(fee) for symbol, fee in sorted(schedule.symbol_taker_fees.items())
        },
        "symbol_maker_fees": {
            symbol: str(fee) for symbol, fee in sorted(schedule.symbol_maker_fees.items())
        },
        "symbols": {
            symbol: {
                "maker_fee": str(fee.maker_fee),
                "taker_fee": str(fee.taker_fee),
            }
            for symbol, fee in sorted(schedule.symbol_fees.items())
        },
    }
    try:
        rendered = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(target, rendered)
    except (OSError, TypeError, ValueError, yaml.YAMLError) as exc:
        raise FeeConfigurationError(
            f"could not write normalized MEXC fee config: {target}"
        ) from exc
    _restrict_file_permissions(target)
    return target


def load_fee_schedule(path: str | Path = DEFAULT_NORMALIZED_FEE_PATH) -> FeeSchedule:
    """Safely load a normalized YAML fee schedule."""

    if yaml is None:
        raise FeeConfigurationError("PyYAML is required to load the MEXC fee config")
    source = Path(path)
    try:
        payload = yaml.safe_load(source.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise FeeConfigurationError(
            "MEXC account fee config was not found. "
            "Run python -m tri_arb.tools.check_mexc_fees first."
        ) from exc
    except (OSError, yaml.YAMLError) as exc:
        raise FeeConfigurationError(f"could not read MEXC account fee config: {source}") from exc
    if not isinstance(payload, Mapping):
        raise FeeConfigurationError("normalized MEXC fee config must be a mapping")
    return FeeSchedule.from_mapping(payload)


load_mexc_fee_schedule = load_fee_schedule


def _read_dotenv(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise FeeConfigurationError(f"could not read credential file: {path}") from exc

    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise FeeConfigurationError(
                f"invalid credential file syntax at {path}, line {line_number}"
            )
        key, raw_value = line.split("=", 1)
        key = key.strip()
        value = raw_value.strip()
        if value.startswith(("'", '"')):
            quote = value[0]
            closing = value.find(quote, 1)
            if closing < 0:
                raise FeeConfigurationError(
                    f"invalid credential file syntax at {path}, line {line_number}"
                )
            trailing = value[closing + 1 :].strip()
            if trailing and not trailing.startswith("#"):
                raise FeeConfigurationError(
                    f"invalid credential file syntax at {path}, line {line_number}"
                )
            value = value[1:closing]
        else:
            comment_index = next(
                (
                    index
                    for index in range(1, len(value))
                    if value[index] == "#" and value[index - 1].isspace()
                ),
                -1,
            )
            if comment_index >= 0:
                value = value[:comment_index].rstrip()
        if key in {MEXC_API_KEY_ENV, MEXC_API_SECRET_ENV}:
            values[key] = value
    return values


def _fee_rows(
    payload: Any,
    inherited_symbol: Any | None = None,
) -> list[Mapping[str, Any]]:
    if isinstance(payload, Sequence) and not isinstance(payload, (str, bytes, bytearray)):
        rows: list[Mapping[str, Any]] = []
        for item in payload:
            rows.extend(_fee_rows(item, inherited_symbol))
        return rows
    if not isinstance(payload, Mapping):
        return []
    local_symbol = _first_value(payload, _SYMBOL_KEYS)
    effective_symbol = local_symbol if local_symbol not in (None, "") else inherited_symbol
    if (
        _first_value(payload, _MAKER_KEYS) is not None
        or _first_value(payload, _TAKER_KEYS) is not None
    ):
        row = dict(payload)
        if effective_symbol not in (None, ""):
            row.setdefault("symbol", effective_symbol)
        return [row]
    for key in _FEE_CONTAINER_KEYS:
        container = _first_value(payload, (key,))
        if container is not None:
            rows = _fee_rows(container, effective_symbol)
            if rows:
                return rows
    rows = []
    for raw_symbol, value in payload.items():
        if not isinstance(value, Mapping):
            continue
        if _first_value(value, _MAKER_KEYS) is None and _first_value(value, _TAKER_KEYS) is None:
            continue
        row = dict(value)
        row.setdefault(
            "symbol",
            effective_symbol if effective_symbol not in (None, "") else raw_symbol,
        )
        rows.append(row)
    return rows


def _first_value(values: Mapping[str, Any], keys: Iterable[str]) -> Any | None:
    canonical_values = {
        _canonical_key(str(raw_key)): value
        for raw_key, value in values.items()
        if value not in (None, "")
    }
    for key in keys:
        if key in values and values[key] not in (None, ""):
            return values[key]
        canonical = canonical_values.get(_canonical_key(key))
        if canonical is not None:
            return canonical
    return None


def _canonical_key(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _fee_decimal(
    value: Any,
    label: str,
    *,
    allow_negative: bool = False,
) -> Decimal:
    raw = str(value).strip()
    is_percentage = raw.endswith("%")
    if is_percentage:
        raw = raw[:-1].strip()
    try:
        parsed = Decimal(raw)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise FeeConfigurationError(f"{label} must be a decimal fraction") from exc
    if is_percentage:
        parsed /= Decimal("100")
    if not parsed.is_finite():
        interval = "(-1, 1)" if allow_negative else "[0, 1)"
        raise FeeConfigurationError(f"{label} must be in {interval}")
    valid = (
        Decimal("-1") < parsed < Decimal("1")
        if allow_negative
        else Decimal("0") <= parsed < Decimal("1")
    )
    if not valid:
        interval = "(-1, 1)" if allow_negative else "[0, 1)"
        raise FeeConfigurationError(f"{label} must be in {interval}")
    return parsed


def _retry_after_seconds(value: Any) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not 0 <= parsed < float("inf"):
        return None
    return min(86_400.0, parsed)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _redact(value: Any, sensitive_values: tuple[str, ...]) -> Any:
    if isinstance(value, Mapping):
        return {
            _redact_text(str(key), sensitive_values): _redact(item, sensitive_values)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item, sensitive_values) for item in value]
    if isinstance(value, tuple):
        return [_redact(item, sensitive_values) for item in value]
    if isinstance(value, str):
        return _redact_text(value, sensitive_values)
    return value


def _redact_text(value: str, sensitive_values: tuple[str, ...]) -> str:
    redacted = value
    for sensitive in sensitive_values:
        redacted = redacted.replace(sensitive, "<redacted>")
    return redacted


def _atomic_write(path: Path, text: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(path)
    finally:
        with suppress(FileNotFoundError):
            temporary.unlink()


def _restrict_file_permissions(path: Path) -> None:
    # Windows ACLs are not represented by POSIX mode bits; the files contain
    # fee metadata only and never contain either credential.
    with suppress(OSError):
        path.chmod(0o600)


__all__ = [
    "DEFAULT_ENV_PATH",
    "DEFAULT_EXPLICIT_REQUEST_INTERVAL_SECONDS",
    "DEFAULT_NORMALIZED_FEE_PATH",
    "DEFAULT_RATE_LIMIT_RETRIES",
    "DEFAULT_RAW_FEE_DIR",
    "DEFAULT_RETRY_BASE_DELAY_SECONDS",
    "FEE_SOURCE",
    "MAX_EXPLICIT_SYMBOLS",
    "MAX_RETRY_DELAY_SECONDS",
    "MEXC_API_KEY_ENV",
    "MEXC_API_SECRET_ENV",
    "MEXC_REST_BASE_URL",
    "MEXC_TRADE_FEE_METHOD",
    "MEXC_TRADE_FEE_PATH",
    "AccountFeeError",
    "FeeCheckResult",
    "FeeConfigurationError",
    "FeeError",
    "FeeSafetyError",
    "FeeSchedule",
    "MexcCredentials",
    "MexcFeeAPIError",
    "MexcReadOnlyFeeClient",
    "SymbolFee",
    "build_signed_query",
    "check_mexc_fees",
    "combine_explicit_symbols",
    "deterministic_query_string",
    "load_discovery_symbols",
    "load_fee_schedule",
    "load_mexc_credentials",
    "load_mexc_fee_schedule",
    "normalize_mexc_fee_response",
    "parse_symbols",
    "save_fee_schedule",
    "save_raw_fee_response",
    "sign_query_string",
    "validate_read_only_fee_request",
]
