from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from tri_arb.exchange import PublicExchangeData
from tri_arb.mexc_proto import (
    decode_mexc_depth_frame,
    encode_mexc_depth_frame,
    protobuf_message_classes,
)
from tri_arb.mexc_public import (
    DEFAULT_MEXC_REST_BASE_URL,
    DEFAULT_MEXC_WS_BASE_URL,
    MexcPublicClient,
    mexc_depth_channel,
    normalize_mexc_depth_update,
    normalize_mexc_order_book_snapshot,
    normalize_mexc_symbol_metadata,
    normalize_mexc_tickers,
    symbol_from_mexc_depth_channel,
)


def test_mexc_adapter_is_public_only_and_uses_secure_hosts() -> None:
    client = MexcPublicClient()

    assert isinstance(client, PublicExchangeData)
    assert client.base_url == DEFAULT_MEXC_REST_BASE_URL == "https://api.mexc.com"
    assert client.websocket_base_url == DEFAULT_MEXC_WS_BASE_URL
    assert client.websocket_base_url.startswith("wss://")

    async def reject_private_endpoint() -> None:
        with pytest.raises(ValueError, match="allow-list"):
            await client._request_json("/api/v3/order")
        await client.close()

    asyncio.run(reject_private_endpoint())


def test_mexc_symbol_channel_parsing_and_connection_sharding() -> None:
    channel = mexc_depth_channel(" btcusdt ", interval_ms=100)
    assert channel == "spot@public.aggre.depth.v3.api.pb@100ms@BTCUSDT"
    assert symbol_from_mexc_depth_channel(channel) == "BTCUSDT"

    client = MexcPublicClient(max_streams_per_connection=30)
    shards = client.depth_shards([f"asset{index}usdt" for index in range(61)])
    assert tuple(len(shard) for shard in shards) == (30, 30, 1)
    assert all(symbol == symbol.upper() for shard in shards for symbol in shard)

    with pytest.raises(ValueError, match="at most 30"):
        MexcPublicClient(max_streams_per_connection=31)
    with pytest.raises(ValueError, match="invalid MEXC"):
        symbol_from_mexc_depth_channel("btcusdt@depth")


def test_mexc_exchange_info_normalization_uses_explicit_asset_metadata() -> None:
    payload = {
        "symbols": [
            {
                "symbol": "BTCUSDT",
                "status": "ENABLED",
                "baseAsset": "BTC",
                "quoteAsset": "USDT",
                "baseAssetPrecision": 8,
                "quoteAssetPrecision": 8,
                "baseSizePrecision": "0.000001",
                "quoteAmountPrecision": "1",
                "maxQuoteAmount": "1000000",
                "permissions": ["SPOT"],
                "isSpotTradingAllowed": True,
                "tradeSideType": "1",
                "orderTypes": ["LIMIT", "MARKET"],
            },
            {
                "symbol": "BUYONLYUSDT",
                "status": "1",
                "baseAsset": "BUYONLY",
                "quoteAsset": "USDT",
                "permissions": ["SPOT"],
                "isSpotTradingAllowed": True,
                "tradeSideType": "2",
            },
        ]
    }

    symbols = normalize_mexc_symbol_metadata(payload)
    by_symbol = {symbol.symbol: symbol for symbol in symbols}
    btc = by_symbol["BTCUSDT"]

    assert btc.base_asset == "BTC"
    assert btc.quote_asset == "USDT"
    assert btc.status == "TRADING"
    assert btc.is_tradable_spot
    assert btc.min_qty == Decimal("0.000001")
    assert btc.step_size == Decimal("0.000001")
    assert btc.min_notional == Decimal("1")
    assert btc.max_notional == Decimal("1000000")
    assert not by_symbol["BUYONLYUSDT"].is_spot_trading_allowed


def test_mexc_ticker_normalization_merges_public_book_ticker() -> None:
    tickers = [
        {
            "symbol": "BTCUSDT",
            "volume": "2.5",
            "quoteVolume": "150000",
            "lastPrice": "60000",
            "bidPrice": "",
            "bidQty": "",
            "askPrice": "",
            "askQty": "",
            "count": None,
        }
    ]
    top = [
        {
            "symbol": "BTCUSDT",
            "bidPrice": "59999.9",
            "bidQty": "1.25",
            "askPrice": "60000.1",
            "askQty": "0.75",
        }
    ]

    stats = normalize_mexc_tickers(tickers, book_tickers=top)["BTCUSDT"]
    assert stats.quote_volume == Decimal("150000")
    assert stats.bid_price == Decimal("59999.9")
    assert stats.bid_qty == Decimal("1.25")
    assert stats.ask_price == Decimal("60000.1")
    assert stats.ask_qty == Decimal("0.75")
    assert stats.weighted_average_price == Decimal("60000")
    assert stats.trade_count == 0


@pytest.mark.parametrize("version_key", ["lastUpdateId", "version"])
def test_mexc_depth_snapshot_normalization_accepts_documented_versions(version_key: str) -> None:
    snapshot = normalize_mexc_order_book_snapshot(
        "btcusdt",
        {
            version_key: "101",
            "bids": [["10", "2"]],
            "asks": [["11", "3"]],
            "timestamp": 1234,
        },
    )

    assert snapshot.symbol == "BTCUSDT"
    assert snapshot.version == 101
    assert snapshot.bids == ((Decimal("10"), Decimal("2")),)
    assert snapshot.asks == ((Decimal("11"), Decimal("3")),)
    assert snapshot.as_mapping()["lastUpdateId"] == 101


def test_official_mexc_protobuf_field_numbers_and_depth_decoding() -> None:
    pytest.importorskip("google.protobuf")
    _, depth_class, wrapper_class = protobuf_message_classes()
    assert wrapper_class.DESCRIPTOR.fields_by_name["channel"].number == 1
    assert wrapper_class.DESCRIPTOR.fields_by_name["publicAggreDepths"].number == 313
    assert wrapper_class.DESCRIPTOR.fields_by_name["symbol"].number == 3
    assert wrapper_class.DESCRIPTOR.fields_by_name["sendTime"].number == 6
    assert depth_class.DESCRIPTOR.fields_by_name["asks"].number == 1
    assert depth_class.DESCRIPTOR.fields_by_name["bids"].number == 2
    assert depth_class.DESCRIPTOR.fields_by_name["fromVersion"].number == 4
    assert depth_class.DESCRIPTOR.fields_by_name["toVersion"].number == 5

    payload = encode_mexc_depth_frame(
        symbol="BTCUSDT",
        from_version=102,
        to_version=104,
        bids=(("10", "4"), ("9", "0")),
        asks=(("11", "5"),),
        send_time_ms=1_721_234_567_890,
        last_order_create_time_ms=1_721_234_567_800,
    )
    decoded = decode_mexc_depth_frame(payload)
    normalized = normalize_mexc_depth_update(payload)

    assert decoded.symbol == "BTCUSDT"
    assert decoded.from_version == "102"
    assert decoded.to_version == "104"
    assert decoded.last_order_create_time_ms == 1_721_234_567_800
    assert normalized.from_version == 102
    assert normalized.to_version == 104
    assert normalized.bids == (
        (Decimal("10"), Decimal("4")),
        (Decimal("9"), Decimal("0")),
    )
    assert normalized.asks == ((Decimal("11"), Decimal("5")),)
    assert normalized.event_time_ms == 1_721_234_567_890


def test_mexc_decoded_mapping_requires_consistent_symbol_and_valid_versions() -> None:
    with pytest.raises(ValueError, match="does not match"):
        normalize_mexc_depth_update(
            {
                "channel": "spot@public.aggre.depth.v3.api.pb@100ms@ETHUSDT",
                "symbol": "BTCUSDT",
                "publicAggreDepths": {
                    "fromVersion": "1",
                    "toVersion": "1",
                    "bids": [],
                    "asks": [],
                },
            }
        )

    with pytest.raises(ValueError, match="version range"):
        normalize_mexc_depth_update(
            {
                "symbol": "BTCUSDT",
                "fromVersion": "3",
                "toVersion": "2",
                "bids": [],
                "asks": [],
            }
        )
