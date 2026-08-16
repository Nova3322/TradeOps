from __future__ import annotations

import base64
import hashlib
import hmac
import urllib.parse
from datetime import UTC, datetime
from typing import Any

import pytest

from trading_control_plane.binance import BinanceReadOnlyClient
from trading_control_plane.bybit import BybitReadOnlyClient
from trading_control_plane.domain import DomainRejected
from trading_control_plane.hyperliquid import HyperliquidReadOnlyClient
from trading_control_plane.okx import OkxReadOnlyClient

NOW = datetime(2026, 8, 10, 4, 5, 6, 789000, tzinfo=UTC)


def test_binance_connection_probe_is_one_signed_read_without_fact_ingestion() -> None:
    calls: list[tuple[str, dict[str, str], float]] = []

    def fetcher(url: str, headers: dict[str, str], timeout: float) -> list[dict[str, Any]]:
        calls.append((url, headers, timeout))
        return []

    client = BinanceReadOnlyClient(
        base_url="https://fapi.binance.com",
        api_key="key-1234",
        api_secret="secret-5678",  # noqa: S106 - inert signing fixture
        fetcher=fetcher,
        server_time_fetcher=lambda _timeout: 1_723_000_000_123,
    )

    client.verify_connection(now=NOW)

    assert len(calls) == 1
    url, headers, timeout = calls[0]
    parsed = urllib.parse.urlparse(url)
    query = dict(urllib.parse.parse_qsl(parsed.query))
    assert parsed.path == "/fapi/v3/balance"
    assert headers == {"X-MBX-APIKEY": "key-1234"}
    assert timeout == 5.0
    assert query["timestamp"] == "1723000000123"
    unsigned = urllib.parse.urlencode(
        {
            "recvWindow": query["recvWindow"],
            "timestamp": query["timestamp"],
        }
    )
    assert (
        query["signature"]
        == hmac.new(b"secret-5678", unsigned.encode(), hashlib.sha256).hexdigest()
    )


def test_hyperliquid_connection_probe_uses_only_public_account_info() -> None:
    calls: list[dict[str, Any]] = []

    def fetcher(_url: str, payload: dict[str, Any], _timeout: float) -> dict[str, Any]:
        calls.append(payload)
        return {"marginSummary": {}, "assetPositions": []}

    client = HyperliquidReadOnlyClient(
        base_url="https://api.hyperliquid.xyz",
        account_address="0x1111111111111111111111111111111111111111",
        fetcher=fetcher,
    )

    client.verify_connection(now=NOW)

    assert calls == [
        {
            "type": "clearinghouseState",
            "user": "0x1111111111111111111111111111111111111111",
        }
    ]


def test_okx_connection_probe_uses_official_v5_signature_contract() -> None:
    calls: list[tuple[str, dict[str, str], float]] = []

    def fetcher(url: str, headers: dict[str, str], timeout: float) -> dict[str, Any]:
        calls.append((url, headers, timeout))
        return {"code": "0", "data": []}

    client = OkxReadOnlyClient(
        api_key="okx-key",
        api_secret="okx-secret",  # noqa: S106 - inert signing fixture
        passphrase="okx-passphrase",  # noqa: S106 - inert signing fixture
        fetcher=fetcher,
    )

    client.verify_connection(now=NOW)

    assert len(calls) == 1
    url, headers, timeout = calls[0]
    timestamp = "2026-08-10T04:05:06.789Z"
    signature = base64.b64encode(
        hmac.new(
            b"okx-secret",
            f"{timestamp}GET/api/v5/account/balance".encode(),
            hashlib.sha256,
        ).digest()
    ).decode()
    assert url == "https://www.okx.com/api/v5/account/balance"
    assert headers == {
        "OK-ACCESS-KEY": "okx-key",
        "OK-ACCESS-SIGN": signature,
        "OK-ACCESS-TIMESTAMP": timestamp,
        "OK-ACCESS-PASSPHRASE": "okx-passphrase",
    }
    assert timeout == 5.0


def test_bybit_connection_probe_uses_official_v5_signature_contract() -> None:
    calls: list[tuple[str, dict[str, str], float]] = []

    def fetcher(url: str, headers: dict[str, str], timeout: float) -> dict[str, Any]:
        calls.append((url, headers, timeout))
        return {"retCode": 0, "result": {"list": []}}

    client = BybitReadOnlyClient(
        api_key="bybit-key",
        api_secret="bybit-secret",  # noqa: S106 - inert signing fixture
        fetcher=fetcher,
    )

    client.verify_connection(now=NOW)

    assert len(calls) == 1
    url, headers, timeout = calls[0]
    timestamp = str(int(NOW.timestamp() * 1000))
    payload = f"{timestamp}bybit-key5000accountType=UNIFIED"
    assert url == "https://api.bybit.com/v5/account/wallet-balance?accountType=UNIFIED"
    assert headers == {
        "X-BAPI-API-KEY": "bybit-key",
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-RECV-WINDOW": "5000",
        "X-BAPI-SIGN": hmac.new(b"bybit-secret", payload.encode(), hashlib.sha256).hexdigest(),
    }
    assert timeout == 5.0


@pytest.mark.parametrize(
    ("client", "code"),
    [
        (
            OkxReadOnlyClient(
                api_key="key",
                api_secret="secret",  # noqa: S106 - inert fixture
                passphrase="phrase",  # noqa: S106 - inert fixture
                fetcher=lambda *_args: {"code": "50113", "data": []},
            ),
            "OKX_AUTHENTICATION_FAILED",
        ),
        (
            BybitReadOnlyClient(
                api_key="key",
                api_secret="secret",  # noqa: S106 - inert fixture
                fetcher=lambda *_args: {"retCode": 10006, "result": {}},
            ),
            "BYBIT_RATE_LIMITED",
        ),
    ],
)
def test_connection_probe_rejections_use_stable_non_secret_error_codes(
    client: OkxReadOnlyClient | BybitReadOnlyClient,
    code: str,
) -> None:
    with pytest.raises(DomainRejected, match=code):
        client.verify_connection(now=NOW)


def test_okx_and_bybit_clients_reject_non_official_hosts() -> None:
    with pytest.raises(ValueError, match="official API host"):
        OkxReadOnlyClient(
            base_url="https://example.com",
            api_key="key",
            api_secret="secret",  # noqa: S106 - inert fixture
            passphrase="phrase",  # noqa: S106 - inert fixture
        )
    with pytest.raises(ValueError, match="official API host"):
        BybitReadOnlyClient(
            base_url="https://example.com",
            api_key="key",
            api_secret="secret",  # noqa: S106 - inert fixture
        )
