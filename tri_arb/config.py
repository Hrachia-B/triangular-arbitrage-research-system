"""Typed configuration loading for the research-only simulator.

The public API deliberately uses standard-library dataclasses.  YAML is only
decoded at the boundary, which keeps CLI overrides and unit tests lightweight and
makes every effective value available to the final report.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

DEFAULT_BRIDGE_ASSETS = (
    "BTC",
    "ETH",
    "BNB",
    "SOL",
    "XRP",
    "DOGE",
    "ADA",
    "AVAX",
    "LINK",
    "TRX",
    "LTC",
    "BCH",
    "DOT",
    "MATIC",
    "POL",
)

SUPPORTED_EXCHANGES = frozenset({"binance", "mexc"})
DEFAULT_NETWORK_ENDPOINTS = {
    "binance": (
        "https://data-api.binance.vision",
        "wss://data-stream.binance.vision:443",
    ),
    "mexc": (
        "https://api.mexc.com",
        "wss://wbs-api.mexc.com/ws",
    ),
}
DEFAULT_FEE_SENSITIVITY_RATES = {
    "binance": (
        Decimal("0.001"),
        Decimal("0.00075"),
        Decimal("0.0005"),
        Decimal("0.0002"),
        Decimal("0.0001"),
        Decimal("0"),
    ),
    "mexc": (
        Decimal("0.001"),
        Decimal("0.0005"),
        Decimal("0.0002"),
        Decimal("0.0001"),
        Decimal("0"),
    ),
}
MEXC_DEFAULT_EXCLUDE_SYMBOL_PATTERNS = (
    "*3L*",
    "*3S*",
    "*5L*",
    "*5S*",
    "*BULL*",
    "*BEAR*",
)

ZERO = Decimal("0")


class ConfigError(ValueError):
    """Raised when a configuration value is missing, unknown, or unsafe."""


def _decimal(value: Any, *, name: str) -> Decimal:
    if isinstance(value, Decimal):
        result = value
    else:
        try:
            result = Decimal(str(value).strip())
        except (InvalidOperation, ValueError, AttributeError) as exc:
            raise ConfigError(f"{name} must be a decimal number, got {value!r}") from exc
    if not result.is_finite():
        raise ConfigError(f"{name} must be finite")
    return result


def _integer(value: Any, *, name: str) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"{name} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be an integer, got {value!r}") from exc
    if str(value).strip() not in {str(result), f"+{result}"} and not isinstance(value, int):
        # Reject silent truncation of values such as 3.5 while allowing "003".
        try:
            if Decimal(str(value)) != Decimal(result):
                raise ConfigError(f"{name} must be an integer, got {value!r}")
        except InvalidOperation as exc:
            raise ConfigError(f"{name} must be an integer, got {value!r}") from exc
    return result


def _number(value: Any, *, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be a number, got {value!r}") from exc
    if result != result or result in (float("inf"), float("-inf")):
        raise ConfigError(f"{name} must be finite")
    return result


def _bool(value: Any, *, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "on", "1"}:
            return True
        if normalized in {"false", "no", "off", "0"}:
            return False
    if value in (0, 1):
        return bool(value)
    raise ConfigError(f"{name} must be a boolean, got {value!r}")


def _sequence(value: Any, *, name: str) -> tuple[Any, ...]:
    if isinstance(value, str):
        return tuple(part.strip() for part in value.split(",") if part.strip())
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return tuple(value)
    raise ConfigError(f"{name} must be a list or comma-separated string")


def _assets(value: Any, *, name: str) -> tuple[str, ...]:
    assets = tuple(str(item).strip().upper() for item in _sequence(value, name=name))
    if any(not asset for asset in assets):
        raise ConfigError(f"{name} cannot contain an empty asset")
    return tuple(dict.fromkeys(assets))


def _patterns(value: Any, *, name: str) -> tuple[str, ...]:
    patterns = tuple(str(item).strip().upper() for item in _sequence(value, name=name))
    if any(not pattern for pattern in patterns):
        raise ConfigError(f"{name} cannot contain an empty pattern")
    return tuple(dict.fromkeys(patterns))


def _optional_decimal(value: Any, *, name: str) -> Decimal | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return _decimal(value, name=name)


def _exchange(value: Any) -> str:
    exchange = str(value).strip().lower()
    if exchange not in SUPPORTED_EXCHANGES:
        choices = ", ".join(sorted(SUPPORTED_EXCHANGES))
        raise ConfigError(f"exchange must be one of: {choices}")
    return exchange


@dataclass(frozen=True, slots=True)
class RunConfig:
    duration_minutes: float = 60.0
    checkpoint_every_minutes: float = 60.0

    def __post_init__(self) -> None:
        if self.duration_minutes <= 0:
            raise ConfigError("run.duration_minutes must be positive")
        if self.checkpoint_every_minutes < 0:
            raise ConfigError("run.checkpoint_every_minutes cannot be negative")


@dataclass(frozen=True, slots=True)
class DiscoveryConfig:
    root_asset: str = "USDT"
    important_bridge_assets: tuple[str, ...] = DEFAULT_BRIDGE_ASSETS
    max_symbols: int = 50
    max_cycles: int = 50
    # Compatibility threshold in each market's native quote asset. Prefer the
    # USDT-denominated threshold when comparing cross-quoted markets.
    min_quote_volume: Decimal = Decimal("0")
    require_usable_ticker_book: bool = True
    exclude_assets: tuple[str, ...] = ()
    min_quote_volume_usdt: Decimal = Decimal("0")
    max_spread_bps: Decimal | None = None
    min_top_of_book_notional: Decimal = Decimal("0")
    exclude_symbol_patterns: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        normalized_root = self.root_asset.strip().upper()
        if not normalized_root:
            raise ConfigError("discovery.root_asset cannot be empty")
        object.__setattr__(self, "root_asset", normalized_root)
        normalized_bridges = tuple(
            asset
            for asset in dict.fromkeys(a.strip().upper() for a in self.important_bridge_assets)
            if asset and asset != normalized_root
        )
        object.__setattr__(self, "important_bridge_assets", normalized_bridges)
        object.__setattr__(
            self,
            "exclude_assets",
            tuple(dict.fromkeys(a.strip().upper() for a in self.exclude_assets if a.strip())),
        )
        object.__setattr__(
            self,
            "exclude_symbol_patterns",
            tuple(
                dict.fromkeys(
                    pattern.strip().upper()
                    for pattern in self.exclude_symbol_patterns
                    if pattern.strip()
                )
            ),
        )
        if self.max_symbols < 3:
            raise ConfigError("discovery.max_symbols must be at least 3")
        if self.max_cycles < 1:
            raise ConfigError("discovery.max_cycles must be positive")
        if self.min_quote_volume < 0:
            raise ConfigError("discovery.min_quote_volume cannot be negative")
        if self.min_quote_volume_usdt < 0:
            raise ConfigError("discovery.min_quote_volume_usdt cannot be negative")
        if self.max_spread_bps is not None and self.max_spread_bps < 0:
            raise ConfigError("discovery.max_spread_bps cannot be negative")
        if self.min_top_of_book_notional < 0:
            raise ConfigError("discovery.min_top_of_book_notional cannot be negative")


@dataclass(frozen=True, slots=True)
class OrderBookConfig:
    depth_levels: int = 100
    snapshot_limit: int = 100
    stream_interval_ms: int = 100
    max_streams_per_connection: int = 50
    stale_after_ms: int = 2_000
    startup_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.depth_levels < 1:
            raise ConfigError("order_book.depth_levels must be positive")
        if self.snapshot_limit not in {5, 10, 20, 50, 100, 500, 1000, 5000}:
            raise ConfigError("order_book.snapshot_limit is not supported")
        if self.depth_levels > self.snapshot_limit:
            raise ConfigError("order_book.depth_levels cannot exceed snapshot_limit")
        if self.stream_interval_ms not in {10, 100, 1000}:
            raise ConfigError("order_book.stream_interval_ms must be 10, 100, or 1000")
        if not 1 <= self.max_streams_per_connection <= 1024:
            raise ConfigError("order_book.max_streams_per_connection must be between 1 and 1024")
        if self.stale_after_ms < 1 or self.startup_timeout_seconds <= 0:
            raise ConfigError("order book timeouts must be positive")

    @property
    def stream_suffix(self) -> str:
        return f"@depth@{self.stream_interval_ms}ms"


@dataclass(frozen=True, slots=True)
class SimulationConfig:
    fee_rate: Decimal = Decimal("0.001")
    start_sizes: tuple[Decimal, ...] = (
        Decimal("10"),
        Decimal("25"),
        Decimal("50"),
        Decimal("100"),
    )
    latency_buckets_ms: tuple[int, ...] = (50, 100, 250, 500, 1000)
    quantity_haircut: Decimal = Decimal("0.25")
    extra_slippage_bps: Decimal = Decimal("1")
    scan_interval_ms: int = 50
    signal_cooldown_ms: int = 1_000
    profit_threshold_bps: Decimal = Decimal("0")
    fee_sensitivity_rates: tuple[Decimal, ...] = DEFAULT_FEE_SENSITIVITY_RATES["binance"]

    def __post_init__(self) -> None:
        if not ZERO <= self.fee_rate < Decimal("1"):
            raise ConfigError("simulation.fee_rate must be in [0, 1)")
        if not self.start_sizes or any(size <= 0 for size in self.start_sizes):
            raise ConfigError("simulation.start_sizes must contain positive amounts")
        if not self.latency_buckets_ms:
            raise ConfigError("simulation.latency_buckets_ms cannot be empty")
        if any(delay < 0 for delay in self.latency_buckets_ms):
            raise ConfigError("simulation.latency_buckets_ms cannot contain negatives")
        object.__setattr__(self, "latency_buckets_ms", tuple(sorted(set(self.latency_buckets_ms))))
        if not self.fee_sensitivity_rates:
            raise ConfigError("simulation.fee_sensitivity_rates cannot be empty")
        if any(not ZERO <= rate < Decimal("1") for rate in self.fee_sensitivity_rates):
            raise ConfigError("simulation.fee_sensitivity_rates must be in [0, 1)")
        object.__setattr__(
            self,
            "fee_sensitivity_rates",
            tuple(dict.fromkeys(self.fee_sensitivity_rates)),
        )
        if not ZERO <= self.quantity_haircut < Decimal("1"):
            raise ConfigError("simulation.quantity_haircut must be in [0, 1)")
        if not ZERO <= self.extra_slippage_bps < Decimal("10000"):
            raise ConfigError("simulation.extra_slippage_bps must be in [0, 10000)")
        if self.scan_interval_ms < 1 or self.signal_cooldown_ms < 0:
            raise ConfigError("simulation scan timing cannot be negative")


@dataclass(frozen=True, slots=True)
class NetworkConfig:
    # Effective defaults are selected from ``exchange`` by config_from_mapping.
    rest_base_url: str = "https://data-api.binance.vision"
    websocket_base_url: str = "wss://data-stream.binance.vision:443"
    request_timeout_seconds: float = 10.0
    max_retries: int = 5
    retry_base_delay_seconds: float = 0.5

    def __post_init__(self) -> None:
        rest = self.rest_base_url.rstrip("/")
        websocket = self.websocket_base_url.rstrip("/")
        if not rest.startswith("https://"):
            raise ConfigError("network.rest_base_url must use HTTPS")
        if not websocket.startswith("wss://"):
            raise ConfigError("network.websocket_base_url must use WSS")
        object.__setattr__(self, "rest_base_url", rest)
        object.__setattr__(self, "websocket_base_url", websocket)
        if self.request_timeout_seconds <= 0 or self.retry_base_delay_seconds < 0:
            raise ConfigError("network timeouts must be positive")
        if self.max_retries < 0:
            raise ConfigError("network.max_retries cannot be negative")

    @property
    def ws_base_url(self) -> str:
        return self.websocket_base_url


@dataclass(frozen=True, slots=True)
class OutputConfig:
    output_dir: Path = Path("data")
    log_level: str = "INFO"
    max_jsonl_bytes: int = 50 * 1024 * 1024
    log_backup_count: int = 3
    storage_mode: str = "full"
    min_free_gib: float | None = None
    raw_sample_rate: float = 0.001
    top_n: int = 1_000
    near_break_even_threshold: Decimal = Decimal("-0.0005")

    def __post_init__(self) -> None:
        object.__setattr__(self, "output_dir", Path(self.output_dir))
        normalized_level = self.log_level.strip().upper()
        if normalized_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigError(f"unsupported output.log_level: {self.log_level!r}")
        object.__setattr__(self, "log_level", normalized_level)
        if self.max_jsonl_bytes < 1 or self.log_backup_count < 0:
            raise ConfigError("output rotation settings cannot be negative")
        storage_mode = self.storage_mode.strip().lower()
        if storage_mode not in {"full", "compact"}:
            raise ConfigError("output.storage_mode must be 'full' or 'compact'")
        object.__setattr__(self, "storage_mode", storage_mode)
        minimum = 15.0 if storage_mode == "compact" else 100.0
        if self.min_free_gib is not None:
            minimum = float(self.min_free_gib)
        if minimum < 0:
            raise ConfigError("output.min_free_gib cannot be negative")
        object.__setattr__(self, "min_free_gib", minimum)
        sample_rate = float(self.raw_sample_rate)
        if not 0 <= sample_rate <= 1:
            raise ConfigError("output.raw_sample_rate must be in [0, 1]")
        object.__setattr__(self, "raw_sample_rate", sample_rate)
        if self.top_n < 1:
            raise ConfigError("output.top_n must be positive")
        object.__setattr__(
            self,
            "near_break_even_threshold",
            Decimal(str(self.near_break_even_threshold)),
        )


@dataclass(frozen=True, slots=True)
class DecisionConfig:
    """Configurable evidence thresholds for the dedicated 48-hour decision."""

    minimum_duration_minutes: float = 2_880.0
    minimum_sample_size: int = 20
    stop_ghost_percentage: Decimal = Decimal("99")
    repeated_positive_cycle_min_signals: int = 2
    minimum_positive_checkpoints: int = 2
    survival_buckets_ms: tuple[int, ...] = (50, 100)
    minimum_total_estimated_pnl: Decimal = ZERO

    def __post_init__(self) -> None:
        if self.minimum_duration_minutes <= 0:
            raise ConfigError("decision.minimum_duration_minutes must be positive")
        if self.minimum_sample_size < 1:
            raise ConfigError("decision.minimum_sample_size must be positive")
        if not ZERO <= self.stop_ghost_percentage <= Decimal("100"):
            raise ConfigError("decision.stop_ghost_percentage must be in [0, 100]")
        if self.repeated_positive_cycle_min_signals < 1:
            raise ConfigError("decision.repeated_positive_cycle_min_signals must be positive")
        if self.minimum_positive_checkpoints < 2:
            raise ConfigError("decision.minimum_positive_checkpoints must be at least 2")
        if not self.survival_buckets_ms or any(value < 0 for value in self.survival_buckets_ms):
            raise ConfigError("decision.survival_buckets_ms must contain non-negative values")
        object.__setattr__(
            self,
            "survival_buckets_ms",
            tuple(sorted(set(self.survival_buckets_ms))),
        )


@dataclass(frozen=True, slots=True)
class AppConfig:
    run: RunConfig = RunConfig()
    discovery: DiscoveryConfig = DiscoveryConfig()
    order_book: OrderBookConfig = OrderBookConfig()
    simulation: SimulationConfig = SimulationConfig()
    network: NetworkConfig = NetworkConfig()
    output: OutputConfig = OutputConfig()
    decision: DecisionConfig = DecisionConfig()
    exchange: str = "binance"

    def __post_init__(self) -> None:
        object.__setattr__(self, "exchange", _exchange(self.exchange))

    @property
    def duration_minutes(self) -> float:
        return self.run.duration_minutes

    @property
    def root_asset(self) -> str:
        return self.discovery.root_asset

    @property
    def max_symbols(self) -> int:
        return self.discovery.max_symbols

    @property
    def max_cycles(self) -> int:
        return self.discovery.max_cycles

    def with_overrides(self, overrides: Mapping[str, Any]) -> AppConfig:
        return config_from_mapping(_merge_overrides(config_to_dict(self), overrides))


Config = AppConfig


_SECTION_ALIASES = {
    "runtime": "run",
    "binance": "network",
    "orderbook": "order_book",
}

_FLAT_KEYS = {
    "duration_minutes": ("run", "duration_minutes"),
    "checkpoint_every_minutes": ("run", "checkpoint_every_minutes"),
    "root_asset": ("discovery", "root_asset"),
    "important_bridge_assets": ("discovery", "important_bridge_assets"),
    "bridge_assets": ("discovery", "important_bridge_assets"),
    "max_symbols": ("discovery", "max_symbols"),
    "max_cycles": ("discovery", "max_cycles"),
    "min_quote_volume": ("discovery", "min_quote_volume"),
    "min_quote_volume_usdt": ("discovery", "min_quote_volume_usdt"),
    "max_spread_bps": ("discovery", "max_spread_bps"),
    "min_top_of_book_notional": ("discovery", "min_top_of_book_notional"),
    "exclude_symbol_patterns": ("discovery", "exclude_symbol_patterns"),
    "depth_levels": ("order_book", "depth_levels"),
    "max_depth_levels": ("order_book", "depth_levels"),
    "snapshot_limit": ("order_book", "snapshot_limit"),
    "max_streams_per_connection": ("order_book", "max_streams_per_connection"),
    "stale_after_ms": ("order_book", "stale_after_ms"),
    "fee_rate": ("simulation", "fee_rate"),
    "start_sizes": ("simulation", "start_sizes"),
    "latency_buckets": ("simulation", "latency_buckets_ms"),
    "latency_buckets_ms": ("simulation", "latency_buckets_ms"),
    "fee_sensitivity_rates": ("simulation", "fee_sensitivity_rates"),
    "quantity_haircut": ("simulation", "quantity_haircut"),
    "extra_slippage_bps": ("simulation", "extra_slippage_bps"),
    "output_dir": ("output", "output_dir"),
    "log_level": ("output", "log_level"),
    "storage_mode": ("output", "storage_mode"),
    "min_free_gib": ("output", "min_free_gib"),
    "raw_sample_rate": ("output", "raw_sample_rate"),
    "top_n": ("output", "top_n"),
    "near_break_even_threshold": ("output", "near_break_even_threshold"),
}

_FIELD_ALIASES = {
    ("network", "ws_base_url"): "websocket_base_url",
    ("order_book", "max_websocket_streams_per_connection"): "max_streams_per_connection",
    ("order_book", "max_depth_levels"): "depth_levels",
    ("simulation", "latency_buckets"): "latency_buckets_ms",
    ("simulation", "displayed_quantity_haircut"): "quantity_haircut",
    ("output", "directory"): "output_dir",
}


def _merge_overrides(base: Mapping[str, Any], overrides: Mapping[str, Any]) -> dict[str, Any]:
    merged = {
        key: dict(value) if isinstance(value, Mapping) else value for key, value in base.items()
    }
    for raw_key, value in overrides.items():
        if value is None:
            continue
        key = str(raw_key)
        if key == "exchange":
            merged[key] = value
            continue
        if "." in key:
            section, field_name = key.split(".", 1)
            section = _SECTION_ALIASES.get(section, section)
            target = (section, _FIELD_ALIASES.get((section, field_name), field_name))
        elif key in _FLAT_KEYS:
            target = _FLAT_KEYS[key]
        elif key in {
            "run",
            "discovery",
            "order_book",
            "simulation",
            "network",
            "output",
            "decision",
        }:
            if not isinstance(value, Mapping):
                raise ConfigError(f"configuration section {key!r} must be a mapping")
            section_values = merged.setdefault(key, {})
            assert isinstance(section_values, dict)
            for nested_key, nested_value in value.items():
                canonical = _FIELD_ALIASES.get((key, str(nested_key)), str(nested_key))
                section_values[canonical] = nested_value
            continue
        elif key in _SECTION_ALIASES:
            section = _SECTION_ALIASES[key]
            if not isinstance(value, Mapping):
                raise ConfigError(f"configuration section {key!r} must be a mapping")
            section_values = merged.setdefault(section, {})
            assert isinstance(section_values, dict)
            for nested_key, nested_value in value.items():
                canonical = _FIELD_ALIASES.get((section, str(nested_key)), str(nested_key))
                section_values[canonical] = nested_value
            continue
        else:
            raise ConfigError(f"unknown configuration key: {key}")
        section, field_name = target
        section_values = merged.setdefault(section, {})
        if not isinstance(section_values, dict):
            raise ConfigError(f"configuration section {section!r} must be a mapping")
        section_values[field_name] = value
    return merged


def _unknown_fields(section: str, values: Mapping[str, Any], allowed: set[str]) -> None:
    unknown = sorted(set(values) - allowed)
    if unknown:
        raise ConfigError(f"unknown {section} configuration field(s): {', '.join(unknown)}")


def config_from_mapping(values: Mapping[str, Any] | None = None) -> AppConfig:
    """Build and validate configuration from nested or CLI-style flat values."""

    raw = _merge_overrides({}, values or {})
    _unknown_fields(
        "top-level",
        raw,
        {
            "exchange",
            "run",
            "discovery",
            "order_book",
            "simulation",
            "network",
            "output",
            "decision",
        },
    )
    exchange = _exchange(raw.get("exchange", "binance"))

    run_raw = dict(raw.get("run", {}))
    _unknown_fields("run", run_raw, {"duration_minutes", "checkpoint_every_minutes"})
    run = RunConfig(
        duration_minutes=_number(run_raw.get("duration_minutes", 60), name="run.duration_minutes"),
        checkpoint_every_minutes=_number(
            run_raw.get("checkpoint_every_minutes", 60),
            name="run.checkpoint_every_minutes",
        ),
    )

    discovery_raw = dict(raw.get("discovery", {}))
    _unknown_fields(
        "discovery",
        discovery_raw,
        {
            "root_asset",
            "important_bridge_assets",
            "max_symbols",
            "max_cycles",
            "min_quote_volume",
            "min_quote_volume_usdt",
            "max_spread_bps",
            "min_top_of_book_notional",
            "require_usable_ticker_book",
            "exclude_assets",
            "exclude_symbol_patterns",
        },
    )
    mexc_defaults = exchange == "mexc"
    discovery = DiscoveryConfig(
        root_asset=str(discovery_raw.get("root_asset", "USDT")),
        important_bridge_assets=_assets(
            discovery_raw.get("important_bridge_assets", DEFAULT_BRIDGE_ASSETS),
            name="discovery.important_bridge_assets",
        ),
        max_symbols=_integer(discovery_raw.get("max_symbols", 50), name="discovery.max_symbols"),
        max_cycles=_integer(discovery_raw.get("max_cycles", 50), name="discovery.max_cycles"),
        min_quote_volume=_decimal(
            discovery_raw.get("min_quote_volume", 0), name="discovery.min_quote_volume"
        ),
        min_quote_volume_usdt=_decimal(
            discovery_raw.get("min_quote_volume_usdt", 100_000 if mexc_defaults else 0),
            name="discovery.min_quote_volume_usdt",
        ),
        max_spread_bps=_optional_decimal(
            discovery_raw.get("max_spread_bps", 50 if mexc_defaults else None),
            name="discovery.max_spread_bps",
        ),
        min_top_of_book_notional=_decimal(
            discovery_raw.get("min_top_of_book_notional", 50 if mexc_defaults else 0),
            name="discovery.min_top_of_book_notional",
        ),
        require_usable_ticker_book=_bool(
            discovery_raw.get("require_usable_ticker_book", True),
            name="discovery.require_usable_ticker_book",
        ),
        exclude_assets=_assets(
            discovery_raw.get("exclude_assets", ()), name="discovery.exclude_assets"
        ),
        exclude_symbol_patterns=_patterns(
            discovery_raw.get(
                "exclude_symbol_patterns",
                MEXC_DEFAULT_EXCLUDE_SYMBOL_PATTERNS if mexc_defaults else (),
            ),
            name="discovery.exclude_symbol_patterns",
        ),
    )

    order_raw = dict(raw.get("order_book", {}))
    _unknown_fields(
        "order_book",
        order_raw,
        {
            "depth_levels",
            "snapshot_limit",
            "stream_interval_ms",
            "max_streams_per_connection",
            "stale_after_ms",
            "startup_timeout_seconds",
        },
    )
    default_max_streams = 30 if exchange == "mexc" else 50
    order_book = OrderBookConfig(
        depth_levels=_integer(order_raw.get("depth_levels", 100), name="order_book.depth_levels"),
        snapshot_limit=_integer(
            order_raw.get("snapshot_limit", 100), name="order_book.snapshot_limit"
        ),
        stream_interval_ms=_integer(
            order_raw.get("stream_interval_ms", 100), name="order_book.stream_interval_ms"
        ),
        max_streams_per_connection=_integer(
            order_raw.get("max_streams_per_connection", default_max_streams),
            name="order_book.max_streams_per_connection",
        ),
        stale_after_ms=_integer(
            order_raw.get("stale_after_ms", 2000), name="order_book.stale_after_ms"
        ),
        startup_timeout_seconds=_number(
            order_raw.get("startup_timeout_seconds", 30),
            name="order_book.startup_timeout_seconds",
        ),
    )
    allowed_intervals = {10, 100} if exchange == "mexc" else {100, 1000}
    if order_book.stream_interval_ms not in allowed_intervals:
        options = ", ".join(str(value) for value in sorted(allowed_intervals))
        raise ConfigError(f"order_book.stream_interval_ms for {exchange} must be one of: {options}")
    if exchange == "mexc" and order_book.max_streams_per_connection > 30:
        raise ConfigError("MEXC supports at most 30 subscriptions per WebSocket connection")

    simulation_raw = dict(raw.get("simulation", {}))
    _unknown_fields(
        "simulation",
        simulation_raw,
        {
            "fee_rate",
            "start_sizes",
            "latency_buckets_ms",
            "fee_sensitivity_rates",
            "quantity_haircut",
            "extra_slippage_bps",
            "scan_interval_ms",
            "signal_cooldown_ms",
            "profit_threshold_bps",
        },
    )
    start_sizes = tuple(
        _decimal(value, name="simulation.start_sizes")
        for value in _sequence(
            simulation_raw.get("start_sizes", (10, 25, 50, 100)),
            name="simulation.start_sizes",
        )
    )
    latency_buckets = tuple(
        _integer(value, name="simulation.latency_buckets_ms")
        for value in _sequence(
            simulation_raw.get("latency_buckets_ms", (50, 100, 250, 500, 1000)),
            name="simulation.latency_buckets_ms",
        )
    )
    fee_sensitivity_rates = tuple(
        _decimal(value, name="simulation.fee_sensitivity_rates")
        for value in _sequence(
            simulation_raw.get(
                "fee_sensitivity_rates",
                DEFAULT_FEE_SENSITIVITY_RATES[exchange],
            ),
            name="simulation.fee_sensitivity_rates",
        )
    )
    simulation = SimulationConfig(
        fee_rate=_decimal(simulation_raw.get("fee_rate", "0.001"), name="simulation.fee_rate"),
        start_sizes=start_sizes,
        latency_buckets_ms=latency_buckets,
        fee_sensitivity_rates=fee_sensitivity_rates,
        quantity_haircut=_decimal(
            simulation_raw.get("quantity_haircut", "0.25"),
            name="simulation.quantity_haircut",
        ),
        extra_slippage_bps=_decimal(
            simulation_raw.get("extra_slippage_bps", "1"),
            name="simulation.extra_slippage_bps",
        ),
        scan_interval_ms=_integer(
            simulation_raw.get("scan_interval_ms", 50), name="simulation.scan_interval_ms"
        ),
        signal_cooldown_ms=_integer(
            simulation_raw.get("signal_cooldown_ms", 1000),
            name="simulation.signal_cooldown_ms",
        ),
        profit_threshold_bps=_decimal(
            simulation_raw.get("profit_threshold_bps", 0),
            name="simulation.profit_threshold_bps",
        ),
    )

    network_raw = dict(raw.get("network", {}))
    _unknown_fields(
        "network",
        network_raw,
        {
            "rest_base_url",
            "websocket_base_url",
            "request_timeout_seconds",
            "max_retries",
            "retry_base_delay_seconds",
        },
    )
    default_rest_base_url, default_websocket_base_url = DEFAULT_NETWORK_ENDPOINTS[exchange]
    network = NetworkConfig(
        rest_base_url=str(network_raw.get("rest_base_url", default_rest_base_url)),
        websocket_base_url=str(network_raw.get("websocket_base_url", default_websocket_base_url)),
        request_timeout_seconds=_number(
            network_raw.get("request_timeout_seconds", 10),
            name="network.request_timeout_seconds",
        ),
        max_retries=_integer(network_raw.get("max_retries", 5), name="network.max_retries"),
        retry_base_delay_seconds=_number(
            network_raw.get("retry_base_delay_seconds", 0.5),
            name="network.retry_base_delay_seconds",
        ),
    )

    output_raw = dict(raw.get("output", {}))
    _unknown_fields(
        "output",
        output_raw,
        {
            "output_dir",
            "log_level",
            "max_jsonl_bytes",
            "log_backup_count",
            "storage_mode",
            "min_free_gib",
            "raw_sample_rate",
            "top_n",
            "near_break_even_threshold",
        },
    )
    output = OutputConfig(
        output_dir=Path(output_raw.get("output_dir", "data")),
        log_level=str(output_raw.get("log_level", "INFO")),
        max_jsonl_bytes=_integer(
            output_raw.get("max_jsonl_bytes", 50 * 1024 * 1024),
            name="output.max_jsonl_bytes",
        ),
        log_backup_count=_integer(
            output_raw.get("log_backup_count", 3), name="output.log_backup_count"
        ),
        storage_mode=str(output_raw.get("storage_mode", "full")),
        min_free_gib=(
            None
            if output_raw.get("min_free_gib") is None
            else _number(output_raw["min_free_gib"], name="output.min_free_gib")
        ),
        raw_sample_rate=_number(
            output_raw.get("raw_sample_rate", 0.001), name="output.raw_sample_rate"
        ),
        top_n=_integer(output_raw.get("top_n", 1_000), name="output.top_n"),
        near_break_even_threshold=_decimal(
            output_raw.get("near_break_even_threshold", "-0.0005"),
            name="output.near_break_even_threshold",
        ),
    )
    decision_raw = dict(raw.get("decision", {}))
    _unknown_fields(
        "decision",
        decision_raw,
        {
            "minimum_duration_minutes",
            "minimum_sample_size",
            "stop_ghost_percentage",
            "repeated_positive_cycle_min_signals",
            "minimum_positive_checkpoints",
            "survival_buckets_ms",
            "minimum_total_estimated_pnl",
        },
    )
    decision = DecisionConfig(
        minimum_duration_minutes=_number(
            decision_raw.get("minimum_duration_minutes", 2_880),
            name="decision.minimum_duration_minutes",
        ),
        minimum_sample_size=_integer(
            decision_raw.get("minimum_sample_size", 20),
            name="decision.minimum_sample_size",
        ),
        stop_ghost_percentage=_decimal(
            decision_raw.get("stop_ghost_percentage", 99),
            name="decision.stop_ghost_percentage",
        ),
        repeated_positive_cycle_min_signals=_integer(
            decision_raw.get("repeated_positive_cycle_min_signals", 2),
            name="decision.repeated_positive_cycle_min_signals",
        ),
        minimum_positive_checkpoints=_integer(
            decision_raw.get("minimum_positive_checkpoints", 2),
            name="decision.minimum_positive_checkpoints",
        ),
        survival_buckets_ms=tuple(
            _integer(value, name="decision.survival_buckets_ms")
            for value in _sequence(
                decision_raw.get("survival_buckets_ms", (50, 100)),
                name="decision.survival_buckets_ms",
            )
        ),
        minimum_total_estimated_pnl=_decimal(
            decision_raw.get("minimum_total_estimated_pnl", 0),
            name="decision.minimum_total_estimated_pnl",
        ),
    )
    return AppConfig(
        exchange=exchange,
        run=run,
        discovery=discovery,
        order_book=order_book,
        simulation=simulation,
        network=network,
        output=output,
        decision=decision,
    )


def load_config(
    path: str | Path | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> AppConfig:
    """Load defaults, optional YAML, then optional CLI overrides.

    ``overrides`` accepts dotted keys (``"discovery.max_cycles"``), common flat
    CLI names (``"max_cycles"``), or nested section mappings. ``None`` values are
    ignored so a raw ``argparse`` namespace can be filtered with minimal work.
    """

    raw: Mapping[str, Any] = {}
    if path is not None:
        config_path = Path(path)
        if not config_path.is_file():
            raise ConfigError(f"configuration file does not exist: {config_path}")
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover - exercised in minimal installs
            raise ConfigError("PyYAML is required to load YAML configuration files") from exc
        try:
            decoded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ConfigError(f"could not parse YAML configuration {config_path}: {exc}") from exc
        if decoded is None:
            decoded = {}
        if not isinstance(decoded, Mapping):
            raise ConfigError("the YAML document must contain a mapping at its root")
        raw = decoded
    merged = _merge_overrides({}, raw)
    if overrides:
        merged = _merge_overrides(merged, overrides)
    return config_from_mapping(merged)


def config_to_dict(config: AppConfig) -> dict[str, Any]:
    """Return a YAML/JSON-friendly record with Decimals kept as exact strings."""

    def convert(value: Any) -> Any:
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, Mapping):
            return {key: convert(item) for key, item in value.items()}
        if isinstance(value, (tuple, list)):
            return [convert(item) for item in value]
        return value

    return convert(asdict(config))


def config_with_overrides(config: AppConfig, **overrides: Any) -> AppConfig:
    """Convenience helper for CLI code using keyword-style flat overrides."""

    return config.with_overrides(overrides)
