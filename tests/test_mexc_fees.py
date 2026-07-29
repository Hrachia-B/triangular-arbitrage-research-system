from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

from tri_arb.account_fees import (
    FEE_SOURCE,
    AccountFeeError,
    FeeConfigurationError,
    FeeSafetyError,
    FeeSchedule,
    MexcCredentials,
    MexcFeeAPIError,
    MexcReadOnlyFeeClient,
    build_signed_query,
    check_mexc_fees,
    combine_explicit_symbols,
    deterministic_query_string,
    load_fee_schedule,
    load_mexc_credentials,
    normalize_mexc_fee_response,
    save_raw_fee_response,
    validate_read_only_fee_request,
)


class FakeResponse:
    def __init__(self, payload, status=200, headers=None):
        self.payload = payload
        self.status = status
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc_info):
        return None

    async def json(self, **_kwargs):
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        return response if isinstance(response, FakeResponse) else FakeResponse(response)


def test_signature_is_deterministic_lowercase_hmac_sha256():
    secret = "unit-test-secret"
    query = deterministic_query_string({"timestamp": 1_700_000_000_000, "symbol": "BTCUSDT"})

    assert query == "symbol=BTCUSDT&timestamp=1700000000000"
    expected = hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    assert (
        build_signed_query(
            {"timestamp": 1_700_000_000_000, "symbol": "BTCUSDT"},
            secret,
        )
        == f"{query}&signature={expected}"
    )
    assert expected == expected.lower()


def test_normalizes_wrapped_list_single_row_and_symbol_keyed_payloads():
    wrapped = {
        "code": 0,
        "data": [
            {
                "symbol": "BTCUSDT",
                "makerCommission": "0.0001",
                "takerCommission": "0.0002",
            },
            {
                "symbol": "ETHUSDT",
                "makerFeeRate": "0.02%",
                "takerFeeRate": "0.05%",
            },
        ],
    }
    keyed = {
        "SOLUSDT": {
            "maker_fee": "0",
            "taker_fee": "0.001",
        }
    }

    fees = normalize_mexc_fee_response(wrapped)
    single = normalize_mexc_fee_response(
        {"makerCommission": "0", "takerCommission": "0.0004"},
        requested_symbol="XRPUSDT",
    )
    keyed_fee = normalize_mexc_fee_response(keyed)
    inherited_symbol = normalize_mexc_fee_response(
        {
            "symbol": "ADAUSDT",
            "commission": {
                "maker_commission": "0.0001",
                "taker_commission": "0.0006",
            },
        }
    )

    assert [(fee.symbol, fee.taker_fee) for fee in fees] == [
        ("BTCUSDT", Decimal("0.0002")),
        ("ETHUSDT", Decimal("0.0005")),
    ]
    assert single[0].symbol == "XRPUSDT"
    assert keyed_fee[0].taker_fee == Decimal("0.001")
    assert inherited_symbol[0].symbol == "ADAUSDT"
    assert inherited_symbol[0].taker_fee == Decimal("0.0006")


def test_missing_credentials_fails_clearly_without_values(tmp_path):
    with pytest.raises(
        FeeConfigurationError,
        match="Missing MEXC_API_KEY and MEXC_API_SECRET",
    ):
        load_mexc_credentials(environ={}, env_path=tmp_path / ".env")


def test_environment_precedes_dotenv_and_credentials_repr_is_redacted(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "MEXC_API_KEY=file-key\nMEXC_API_SECRET=file-secret\n",
        encoding="utf-8",
    )

    credentials = load_mexc_credentials(
        environ={
            "MEXC_API_KEY": "environment-key",
            "MEXC_API_SECRET": "environment-secret",
        },
        env_path=env_file,
    )

    rendered = repr(credentials)
    assert credentials.api_key == "environment-key"
    assert credentials.api_secret == "environment-secret"
    assert "environment-key" not in rendered
    assert "environment-secret" not in rendered


def test_complete_environment_does_not_parse_dotenv_and_windows_dotenv_is_supported(tmp_path):
    malformed = tmp_path / "malformed.env"
    malformed.write_text("this line is malformed", encoding="utf-8")
    environment_credentials = load_mexc_credentials(
        environ={
            "MEXC_API_KEY": " environment-key ",
            "MEXC_API_SECRET": " environment-secret ",
        },
        env_path=malformed,
    )

    windows_env = tmp_path / "windows.env"
    windows_env.write_bytes(
        b'\xef\xbb\xbfMEXC_API_KEY="file-key" # public key\r\n'
        b"MEXC_API_SECRET=file-secret # keep private\r\n"
    )
    file_credentials = load_mexc_credentials(environ={}, env_path=windows_env)

    assert environment_credentials.api_key == "environment-key"
    assert environment_credentials.api_secret == "environment-secret"
    assert file_credentials.api_key == "file-key"
    assert file_credentials.api_secret == "file-secret"


def test_all_fees_is_one_get_and_secret_is_not_logged_or_saved(tmp_path, caplog, capsys):
    key = "key-that-must-not-appear"
    secret = "secret-that-must-not-appear"
    response = [
        {
            "symbol": "BTCUSDT",
            "makerCommission": "0.0001",
            "takerCommission": "0.0002",
            "serverNote": secret,
            key: "credential-shaped mapping key",
        },
        {
            "symbol": "ETHUSDT",
            "makerCommission": "0",
            "takerCommission": "0.0005",
        },
    ]
    session = FakeSession([response])
    credentials = MexcCredentials(key, secret)
    client = MexcReadOnlyFeeClient(
        credentials,
        session=session,
        timestamp_provider=lambda: 1_700_000_000_000,
    )

    result = asyncio.run(
        check_mexc_fees(
            credentials,
            client=client,
            raw_output_dir=tmp_path / "data/account",
            config_output_path=tmp_path / "configs/generated/mexc_account_fee.yaml",
            now=datetime(2026, 7, 29, 12, 0, tzinfo=UTC),
        )
    )
    captured = capsys.readouterr()

    assert len(session.calls) == 1
    url, kwargs = session.calls[0]
    parts = urlsplit(url)
    assert parts.scheme == "https"
    assert parts.netloc == "api.mexc.com"
    assert parts.path == "/api/v3/tradeFee"
    assert kwargs["headers"]["X-MEXC-APIKEY"] == key
    query = parse_qs(parts.query)
    assert query["timestamp"] == ["1700000000000"]
    assert "symbol" not in query
    assert len(query["signature"][0]) == 64
    assert result.raw_path.name == "mexc_fees_20260729T120000Z.json"
    assert result.config_path.name == "mexc_account_fee.yaml"
    assert result.schedule.maximum_taker_fee == Decimal("0.0005")

    persisted = result.raw_path.read_text(encoding="utf-8")
    generated = result.config_path.read_text(encoding="utf-8")
    visible = captured.out + captured.err + caplog.text + persisted + generated
    assert key not in visible
    assert secret not in visible
    assert "<redacted>" in persisted
    assert secret not in json.dumps(result.raw_response)
    assert key not in json.dumps(result.raw_response)
    assert secret not in repr(result)
    assert key not in repr(result)


def test_generated_yaml_round_trip_and_conservative_missing_symbol_fallback(tmp_path):
    session = FakeSession(
        [
            {
                "makerCommission": "0",
                "takerCommission": "0.0002",
            },
            {
                "symbol": "ETHUSDT",
                "makerCommission": "0.0001",
                "takerCommission": "0.0007",
            },
        ]
    )
    credentials = MexcCredentials("key", "secret")
    client = MexcReadOnlyFeeClient(credentials, session=session, timestamp_provider=lambda: 1)
    config_path = tmp_path / "configs/generated/mexc_account_fee.yaml"

    result = asyncio.run(
        check_mexc_fees(
            credentials,
            symbols=("BTCUSDT", "ETHUSDT"),
            client=client,
            raw_output_dir=tmp_path / "account",
            config_output_path=config_path,
            now=datetime(2026, 1, 1, tzinfo=UTC),
            explicit_request_interval_seconds=0,
        )
    )
    loaded = load_fee_schedule(config_path)
    raw_yaml = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    assert len(session.calls) == 2
    assert result.requested_symbols == ("BTCUSDT", "ETHUSDT")
    assert loaded.source == FEE_SOURCE
    assert loaded.taker_fee_for("BTCUSDT") == Decimal("0.0002")
    assert loaded.taker_fee_for("unknownusdt") == Decimal("0.0007")
    assert raw_yaml["maximum_taker_fee"] == "0.0007"
    assert "api_key" not in raw_yaml
    assert "api_secret" not in raw_yaml


def test_fee_schedule_never_allows_configured_fallback_below_observed_maximum():
    schedule = FeeSchedule.from_mapping(
        {
            "maximum_taker_fee": "0.0001",
            "symbols": {
                "BTCUSDT": {"maker_fee": "0", "taker_fee": "0.0004"},
            },
        }
    )

    assert schedule.maximum_taker_fee == Decimal("0.0004")
    assert schedule.taker_fee_for("MISSINGUSDT") == Decimal("0.0004")
    assumptions = schedule.to_assumptions()
    assert assumptions["fee_source"] == FEE_SOURCE
    assert assumptions["fallback_taker_fee"] == "0.0004"
    assert assumptions["symbol_taker_fees"] == {"BTCUSDT": "0.0004"}
    assert "secret" not in json.dumps(assumptions).lower()


def test_negative_maker_rebate_is_preserved_but_taker_fee_cannot_be_negative():
    rebate = normalize_mexc_fee_response(
        {
            "symbol": "BTCUSDT",
            "makerCommission": "-0.0001",
            "takerCommission": "0.0002",
        }
    )

    assert rebate[0].maker_fee == Decimal("-0.0001")
    assert rebate[0].taker_fee == Decimal("0.0002")
    with pytest.raises(FeeConfigurationError, match=r"taker fee must be in \[0, 1\)"):
        normalize_mexc_fee_response(
            {
                "symbol": "BTCUSDT",
                "makerCommission": "0",
                "takerCommission": "-0.0001",
            }
        )


def test_fee_schedule_conservatively_merges_redundant_schema_variants():
    schedule = FeeSchedule.from_mapping(
        {
            "fee_source": FEE_SOURCE,
            "fallback_taker_fee": "0.0001",
            "maximum_taker_fee": "0.0003",
            "symbol_taker_fees": {
                "btcusdt": "0.0002",
            },
            "symbol_maker_fees": {
                "BTCUSDT": "0.0001",
            },
            "symbols": {
                "BTCUSDT": {"maker_fee": "0.0004", "taker_fee": "0.0005"},
                "ETHUSDT": {"taker_fee": "0.0004"},
            },
        }
    )

    assert schedule.fallback_taker_fee == Decimal("0.0005")
    assert schedule.symbol_taker_fees == {
        "BTCUSDT": Decimal("0.0005"),
        "ETHUSDT": Decimal("0.0004"),
    }
    assert schedule.symbol_maker_fees["BTCUSDT"] == Decimal("0.0004")


def test_network_errors_and_missing_config_use_safe_account_fee_errors(tmp_path):
    secret = "do-not-leak-this-secret"

    class FailingSession:
        def get(self, *_args, **_kwargs):
            raise OSError(secret)

    credentials = MexcCredentials("key", secret)
    client = MexcReadOnlyFeeClient(
        credentials,
        session=FailingSession(),
        timestamp_provider=lambda: 1,
    )

    with pytest.raises(MexcFeeAPIError) as failure:
        asyncio.run(client.get_trade_fees("BTCUSDT"))
    assert secret not in str(failure.value)

    with pytest.raises(
        AccountFeeError,
        match=r"Run python -m tri_arb\.tools\.check_mexc_fees",
    ):
        load_fee_schedule(tmp_path / "missing.yaml")


def test_explicit_requests_are_paced_and_retry_rate_limits_with_fresh_signatures(tmp_path):
    session = FakeSession(
        [
            FakeResponse({}, status=429, headers={"Retry-After": "0.1"}),
            {"makerCommission": "0", "takerCommission": "0.0002"},
            {"makerCommission": "0", "takerCommission": "0.0003"},
        ]
    )
    timestamps = iter((1, 2, 3))
    credentials = MexcCredentials("key", "secret")
    client = MexcReadOnlyFeeClient(
        credentials,
        session=session,
        timestamp_provider=lambda: next(timestamps),
    )
    delays = []

    async def fake_sleep(delay):
        delays.append(delay)

    result = asyncio.run(
        check_mexc_fees(
            credentials,
            symbols=("BTCUSDT", "ETHUSDT"),
            client=client,
            raw_output_dir=tmp_path / "account",
            config_output_path=tmp_path / "fees.yaml",
            now=datetime(2026, 1, 1, tzinfo=UTC),
            explicit_request_interval_seconds=0.25,
            rate_limit_retries=1,
            sleep=fake_sleep,
        )
    )

    assert len(session.calls) == 3
    assert delays == [0.1, 0.25]
    assert [parse_qs(urlsplit(url).query)["timestamp"][0] for url, _kwargs in session.calls] == [
        "1",
        "2",
        "3",
    ]
    assert result.schedule.fallback_taker_fee == Decimal("0.0003")


def test_long_server_retry_after_aborts_without_spamming(tmp_path):
    session = FakeSession([FakeResponse({}, status=429, headers={"Retry-After": "120"})])
    credentials = MexcCredentials("key", "secret")
    client = MexcReadOnlyFeeClient(
        credentials,
        session=session,
        timestamp_provider=lambda: 1,
    )
    delays = []

    async def fake_sleep(delay):
        delays.append(delay)

    with pytest.raises(MexcFeeAPIError, match="long backoff"):
        asyncio.run(
            check_mexc_fees(
                credentials,
                symbols=("BTCUSDT",),
                client=client,
                raw_output_dir=tmp_path / "account",
                config_output_path=tmp_path / "fees.yaml",
                rate_limit_retries=2,
                sleep=fake_sleep,
            )
        )

    assert len(session.calls) == 1
    assert delays == []
    assert not (tmp_path / "account").exists()


def test_rejected_all_fees_request_explains_documented_symbol_fallback(tmp_path):
    session = FakeSession([FakeResponse({}, status=400)])
    credentials = MexcCredentials("key", "secret")
    client = MexcReadOnlyFeeClient(
        credentials,
        session=session,
        timestamp_provider=lambda: 1,
    )

    with pytest.raises(
        FeeConfigurationError,
        match=r"Rerun with --symbols or --discovery-selection",
    ):
        asyncio.run(
            check_mexc_fees(
                credentials,
                client=client,
                raw_output_dir=tmp_path / "account",
                config_output_path=tmp_path / "fees.yaml",
            )
        )

    assert len(session.calls) == 1
    assert not (tmp_path / "account").exists()


def test_raw_redaction_covers_mapping_keys(tmp_path):
    secret = "mapping-key-secret"
    target = save_raw_fee_response(
        {secret: {"nested": secret}},
        output_dir=tmp_path,
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        sensitive_values=(secret,),
    )

    rendered = target.read_text(encoding="utf-8")
    assert secret not in rendered
    assert rendered.count("<redacted>") == 2


def test_discovery_selection_and_explicit_symbols_are_deduplicated(tmp_path):
    selection = tmp_path / "selected_cycles.json"
    selection.write_text(
        json.dumps(
            {
                "kind": "selected_cycles",
                "records": [
                    {
                        "symbols": ["BTCUSDT", "ETHBTC", "ETHUSDT"],
                        "legs": [{"symbol": "BTCUSDT"}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    assert combine_explicit_symbols("ethusdt,xrpusdt", selection) == (
        "ETHUSDT",
        "XRPUSDT",
        "BTCUSDT",
        "ETHBTC",
    )


def test_hard_endpoint_and_method_boundary():
    validate_read_only_fee_request("GET", "/api/v3/tradeFee")

    with pytest.raises(FeeSafetyError, match="only HTTPS GET"):
        validate_read_only_fee_request("POST", "/api/v3/tradeFee")
    with pytest.raises(FeeSafetyError, match="only HTTPS GET"):
        validate_read_only_fee_request("GET", "/api/v3/" + "order")

    source = Path("tri_arb/account_fees.py").read_text(encoding="utf-8").lower()
    for mutation in ("session.post", "session.delete", "session.put", "session.patch"):
        assert mutation not in source
    for prohibited_path in (
        "/api/v3/order",
        "/api/v3/account",
        "/api/v3/openorders",
        "/fapi/",
        "/dapi/",
        "/sapi/",
    ):
        assert prohibited_path not in source
