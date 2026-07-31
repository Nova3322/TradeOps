from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.error import HTTPError

import pytest

import trading_control_plane.perptape as perptape_module
from trading_control_plane.domain import Direction, DomainRejected
from trading_control_plane.perptape import PerptapeClient

NOW = datetime(2026, 7, 19, 8, tzinfo=UTC)


def response() -> dict[str, object]:
    return {
        "type": "breakouts",
        "generatedAt": 1_784_448_000_000,
        "data": [
            {
                "exchange": "BN",
                "symbol": "BTCUSDT",
                "canonicalSymbol": "BTC",
                "direction": "HH",
                "timeframe": "1h",
                "price": 120000,
                "breakoutPrice": 120000,
                "threshold": 119500,
                "klineReadiness": {"status": "ready"},
                "triggeredAt": 1_784_448_000_000,
                "updatedAt": 1_784_448_030_000,
            },
            {
                "exchange": "HL",
                "symbol": "ETH",
                "canonicalSymbol": "ETH",
                "direction": "LL",
                "timeframe": "4h",
                "price": 2500,
                "breakoutPrice": 2500,
                "threshold": None,
                "klineReadiness": {"status": "stale"},
                "triggeredAt": None,
                "updatedAt": 1_784_448_030_000,
            },
        ],
    }


def test_real_breakout_contract_maps_to_narrow_trading_candidates_and_caches() -> None:
    calls: list[tuple[str, dict[str, str], float]] = []

    def fetch(url: str, headers: dict[str, str], timeout: float) -> dict[str, object]:
        calls.append((url, headers, timeout))
        return response()

    client = PerptapeClient(
        base_url="https://perptape.example",
        api_key="test-api-key",
        contract_version="breakouts-v1",
        cache_ttl=timedelta(minutes=1),
        fetcher=fetch,
    )

    first = client.list_candidates(now=NOW)
    second = client.list_candidates(now=NOW + timedelta(seconds=30))

    assert first == second
    assert len(calls) == 1
    assert calls[0][1] == {
        "authorization": "Bearer test-api-key",
        "x-api-key": "test-api-key",
        "user-agent": "trading-control-plane/1.0",
    }
    assert first[0].venue == "BINANCE"
    assert first[0].direction is Direction.LONG
    assert first[0].readiness == "READY"
    assert first[1].venue == "HYPERLIQUID"
    assert first[1].direction is Direction.SHORT
    assert first[1].data_health == "DEGRADED"
    assert first[0].candidate_id.startswith("pt_")
    assert client.get_candidate(first[0].candidate_id, now=NOW) == first[0]


def test_client_fails_closed_without_api_key_or_with_invalid_contract() -> None:
    missing = PerptapeClient(
        base_url="https://perptape.example",
        api_key=None,
        contract_version="breakouts-v1",
        cache_ttl=timedelta(minutes=1),
    )
    with pytest.raises(DomainRejected, match="PERPTAPE_NOT_CONFIGURED"):
        missing.list_candidates(now=NOW)

    invalid = PerptapeClient(
        base_url="https://perptape.example",
        api_key="key",
        contract_version="breakouts-v1",
        cache_ttl=timedelta(minutes=1),
        fetcher=lambda _url, _headers, _timeout: {"type": "wrong", "data": []},
    )
    with pytest.raises(DomainRejected, match="PERPTAPE_RESPONSE_INVALID"):
        invalid.list_candidates(now=NOW)


def test_candidate_lookup_does_not_invent_missing_source_fact() -> None:
    client = PerptapeClient(
        base_url="https://perptape.example",
        api_key="key",
        contract_version="breakouts-v1",
        cache_ttl=timedelta(minutes=1),
        fetcher=lambda _url, _headers, _timeout: response(),
    )

    with pytest.raises(DomainRejected, match="PERPTAPE_CANDIDATE_NOT_FOUND"):
        client.get_candidate("pt_missing", now=NOW)


def test_invalid_candidate_fact_fails_closed() -> None:
    invalid_response = response()
    data = invalid_response["data"]
    assert isinstance(data, list)
    first = data[0]
    assert isinstance(first, dict)
    first["threshold"] = "not-a-number"
    client = PerptapeClient(
        base_url="https://perptape.example",
        api_key="key",
        contract_version="breakouts-v1",
        cache_ttl=timedelta(minutes=1),
        fetcher=lambda _url, _headers, _timeout: invalid_response,
    )

    with pytest.raises(DomainRejected, match="PERPTAPE_RESPONSE_INVALID"):
        client.list_candidates(now=NOW)


class FakeHttpResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> FakeHttpResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


def test_default_http_transport_maps_success_plan_denial_and_invalid_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        perptape_module.urllib.request,
        "urlopen",
        lambda _request, timeout: FakeHttpResponse(
            b'{"type":"breakouts","generatedAt":1784448000000,"data":[]}'
        ),
    )
    client = PerptapeClient(
        base_url="https://perptape.example",
        api_key="key",
        contract_version="breakouts-v1",
        cache_ttl=timedelta(minutes=1),
    )
    assert client.list_candidates(now=NOW) == []

    def denied(_request: object, timeout: float) -> FakeHttpResponse:
        raise HTTPError("https://perptape.example", 403, "denied", None, None)

    monkeypatch.setattr(perptape_module.urllib.request, "urlopen", denied)
    with pytest.raises(DomainRejected, match="PERPTAPE_PLAN_DENIED"):
        client.list_candidates(now=NOW + timedelta(minutes=2))

    monkeypatch.setattr(
        perptape_module.urllib.request,
        "urlopen",
        lambda _request, timeout: FakeHttpResponse(b"not-json"),
    )
    with pytest.raises(DomainRejected, match="PERPTAPE_RESPONSE_INVALID"):
        client.list_candidates(now=NOW + timedelta(minutes=3))
