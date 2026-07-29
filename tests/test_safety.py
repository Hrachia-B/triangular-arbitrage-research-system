import asyncio
from decimal import Decimal
from pathlib import Path

import pytest

from tri_arb.binance_public import DEFAULT_REST_BASE_URL, BinancePublicClient
from tri_arb.book_manager import DEFAULT_WS_BASE_URL
from tri_arb.config import config_from_mapping, load_config
from tri_arb.exchange import PublicExchangeData

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_market_data_clients_reject_non_allowlisted_endpoint():
    async def scenario():
        client = BinancePublicClient()
        with pytest.raises(ValueError, match="allow-list"):
            await client._request_json("/api/v3/order")
        await client.close()

    asyncio.run(scenario())


def test_defaults_are_binance_market_data_only_hosts():
    assert DEFAULT_REST_BASE_URL == "https://data-api.binance.vision"
    assert DEFAULT_WS_BASE_URL == "wss://data-stream.binance.vision:443"


def test_binance_client_implements_public_exchange_abstraction():
    client = BinancePublicClient()
    assert isinstance(client, PublicExchangeData)
    asyncio.run(client.close())


@pytest.mark.parametrize(
    "config",
    [
        config_from_mapping(),
        load_config(PROJECT_ROOT / "configs" / "default.yaml"),
    ],
)
def test_default_profile_is_bounded_and_research_only(config):
    assert config.duration_minutes == 60
    assert config.discovery.max_symbols == 50
    assert 20 <= config.discovery.max_cycles <= 50
    assert config.order_book.depth_levels == 100
    assert config.order_book.snapshot_limit == 100
    assert config.order_book.max_streams_per_connection == 50
    assert config.order_book.startup_timeout_seconds == 30
    assert config.simulation.fee_rate == Decimal("0.001")
    assert config.simulation.start_sizes == tuple(
        Decimal(value) for value in ("10", "25", "50", "100")
    )
    assert config.simulation.latency_buckets_ms == (50, 100, 250, 500, 1000)
    assert config.simulation.scan_interval_ms == 50
    assert config.simulation.signal_cooldown_ms == 1000
    assert config.network.rest_base_url == DEFAULT_REST_BASE_URL
    assert config.network.websocket_base_url == DEFAULT_WS_BASE_URL
