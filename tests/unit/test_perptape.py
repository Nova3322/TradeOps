from __future__ import annotations

import json
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from io import BytesIO
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import pytest

import trading_control_plane.perptape as perptape_module
from trading_control_plane.domain import Direction, DomainRejected
from trading_control_plane.perptape import (
    PERPTAPE_CANDIDATE_WINDOW,
    PERPTAPE_DECIMAL_MAX_ADJUSTED_EXPONENT,
    PERPTAPE_DECIMAL_MAX_CANONICAL_BYTES,
    PERPTAPE_DECIMAL_MAX_PRECISION,
    PERPTAPE_DECIMAL_MIN_ADJUSTED_EXPONENT,
    PERPTAPE_DECIMAL_MIN_TUPLE_EXPONENT,
    PERPTAPE_PAYLOAD_MAX_BYTES,
    PERPTAPE_STRING_FIELD_MAX_BYTES,
    PerptapeCandidate,
    PerptapeClient,
    PerptapeFeedSnapshot,
    PerptapeRateLimited,
    bound_perptape_feed_snapshot,
    merge_incomplete_perptape_candidates,
    normalize_perptape_datetime,
    perptape_candidate_identity_is_displayable,
    perptape_legacy_candidate_id,
    perptape_payload_size_bytes,
    perptape_snapshot_identity,
    validate_perptape_feed_payload,
)

NOW = datetime(2026, 7, 19, 8, tzinfo=UTC)


def parsed_feed() -> PerptapeFeedSnapshot:
    return PerptapeClient(
        base_url="https://perptape.com",
        api_key="key",
        contract_version="breakouts-v1",
        cache_ttl=timedelta(0),
        fetcher=lambda _url, _headers, _timeout: response(),
    ).refresh(now=NOW)


def test_feed_without_rate_limit_tolerates_sub_millisecond_server_lead() -> None:
    observed_at = NOW + timedelta(microseconds=999)
    payload = response()
    payload["generatedAt"] = int((NOW + timedelta(milliseconds=1)).timestamp() * 1_000)
    client = PerptapeClient(
        base_url="https://perptape.com",
        api_key="key",
        contract_version="breakouts-v1",
        cache_ttl=timedelta(0),
        fetcher=lambda _url, _headers, _timeout: payload,
    )

    snapshot = client.refresh(now=observed_at)

    assert snapshot.generated_at == NOW + timedelta(milliseconds=1)
    assert snapshot.next_allowed_at >= snapshot.generated_at


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
                "volume24hQuote": 1_000_000,
                "openInterestQuote": 500_000,
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
        base_url="https://perptape.com",
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
    assert calls[0][2] == 15
    assert first[0].venue == "BINANCE"
    assert first[0].direction is Direction.LONG
    assert first[0].readiness == "READY"
    assert first[1].venue == "HYPERLIQUID"
    assert first[1].direction is Direction.SHORT
    assert first[1].data_health == "DEGRADED"
    assert first[0].candidate_id.startswith("pt_")
    assert first[0].quote_volume == 1_000_000
    assert first[0].open_interest == 500_000
    assert first[0].detail_url.startswith("https://perptape.com/breakouts?")
    assert "utm_campaign=breakout_signal_symbol" in first[0].detail_url
    assert "/markets?" not in first[0].detail_url
    detail_query = parse_qs(urlparse(first[0].detail_url).query)
    assert detail_query["ex"] == ["BN"]
    assert detail_query["q"] == ["BN:BTCUSDT"]
    assert client.get_candidate(first[0].candidate_id, now=NOW) == first[0]


def test_persisted_legacy_market_scan_link_is_repaired_without_mutating_identity() -> None:
    candidate = parsed_feed().candidates[0]
    value = candidate.to_dict()
    value["detail_url"] = (
        "https://perptape.com/markets?"
        "ex=BN&q=BN%3ABTC%3ABTCUSDT&utm_source=trading_console&"
        "utm_medium=opportunity&utm_campaign=market_scan_symbol&lang=zh-CN"
    )

    restored = PerptapeCandidate.from_dict(value)
    detail_query = parse_qs(urlparse(restored.detail_url).query)

    assert restored.candidate_id == candidate.candidate_id
    assert detail_query["ex"] == ["BN"]
    assert urlparse(restored.detail_url).path == "/breakouts"
    assert detail_query["q"] == ["BN:BTCUSDT"]
    assert detail_query["utm_campaign"] == ["breakout_signal_symbol"]


def test_hyperliquid_hip3_chart_link_preserves_namespace() -> None:
    payload = response()
    hyperliquid = payload["data"][1]
    assert isinstance(hyperliquid, dict)
    hyperliquid["symbol"] = "xyz:IBM"
    hyperliquid["canonicalSymbol"] = "IBM"
    client = PerptapeClient(
        base_url="https://perptape.com",
        api_key="key",
        contract_version="breakouts-v1",
        cache_ttl=timedelta(0),
        fetcher=lambda _url, _headers, _timeout: payload,
    )

    candidate = client.refresh(now=NOW).candidates[1]

    assert candidate.symbol == "xyz:IBM"
    assert candidate.canonical_symbol == "IBM"
    assert candidate.chart_url == "https://app.hyperliquid.xyz/trade/xyz:IBM"


def test_persisted_hyperliquid_hip3_chart_link_is_repaired() -> None:
    candidate = parsed_feed().candidates[1]
    value = candidate.to_dict()
    value.update(
        {
            "symbol": "xyz:IBM",
            "canonical_symbol": "IBM",
            "chart_url": "https://app.hyperliquid.xyz/trade/IBM",
        }
    )

    restored = PerptapeCandidate.from_dict(value)

    assert restored.chart_url == "https://app.hyperliquid.xyz/trade/xyz:IBM"


def test_candidate_identity_distinguishes_contracts_with_same_canonical_symbol() -> None:
    payload = response()
    binance = payload["data"][0]
    assert isinstance(binance, dict)
    alternate_contract = dict(binance)
    alternate_contract["symbol"] = "BTCUSDC"
    payload["data"] = [binance, alternate_contract]

    client = PerptapeClient(
        base_url="https://perptape.com",
        api_key="test-api-key",
        contract_version="breakouts-v1",
        cache_ttl=timedelta(minutes=1),
        fetcher=lambda _url, _headers, _timeout: payload,
    )

    candidates = client.list_candidates(now=NOW)

    assert [candidate.symbol for candidate in candidates] == ["BTCUSDT", "BTCUSDC"]
    assert len({candidate.candidate_id for candidate in candidates}) == 2
    assert len({perptape_legacy_candidate_id(candidate) for candidate in candidates}) == 1
    assert all(
        client.get_candidate(candidate.candidate_id, now=NOW) == candidate
        for candidate in candidates
    )


def test_opportunity_identity_rejects_malformed_binance_symbol_without_guessing() -> None:
    candidate = parsed_feed().candidates[0]

    assert perptape_candidate_identity_is_displayable(candidate) is True
    assert (
        perptape_candidate_identity_is_displayable(
            replace(candidate, symbol="我踏马来了USDT", canonical_symbol="我踏马来了")
        )
        is False
    )
    assert (
        perptape_candidate_identity_is_displayable(
            replace(candidate, venue="HYPERLIQUID", symbol="kPEPE", canonical_symbol="kPEPE")
        )
        is True
    )


def test_authoritative_snapshot_expires_only_old_unresolved_stream_alerts() -> None:
    candidate = parsed_feed().candidates[0]
    old = replace(
        candidate,
        candidate_id="pt_old",
        symbol="OLDUSDT",
        canonical_symbol="OLD",
        triggered_at=NOW - timedelta(minutes=6),
        observed_at=NOW - timedelta(minutes=6),
        readiness="INCOMPLETE",
        data_health="DEGRADED",
    )
    recent = replace(
        old,
        candidate_id="pt_recent",
        symbol="NEWUSDT",
        canonical_symbol="NEW",
        triggered_at=NOW - timedelta(minutes=4),
        observed_at=NOW - timedelta(minutes=4),
    )
    authoritative = replace(
        parsed_feed(),
        generated_at=NOW,
        fetched_at=NOW,
        next_allowed_at=NOW + timedelta(minutes=1),
        candidates=(),
    )

    merged = merge_incomplete_perptape_candidates(
        authoritative,
        (old, recent),
        pending_max_age=timedelta(minutes=5),
    )

    assert [item.symbol for item in merged.candidates] == ["NEWUSDT"]


def test_server_rate_limit_extends_cache_beyond_local_ttl() -> None:
    calls = 0
    payload = response()
    payload["rateLimit"] = {
        "plan": "plus",
        "intervalSeconds": 300,
        "nextAllowedAt": int((NOW + timedelta(minutes=5)).timestamp() * 1_000),
    }

    def fetch(_url: str, _headers: dict[str, str], _timeout: float) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return payload

    client = PerptapeClient(
        base_url="https://perptape.com",
        api_key="test-api-key",
        contract_version="breakouts-v1",
        cache_ttl=timedelta(minutes=1),
        fetcher=fetch,
    )

    first = client.refresh(now=NOW)
    second = client.refresh(now=NOW + timedelta(minutes=2))
    with pytest.raises(PerptapeRateLimited):
        client.refresh(now=NOW + timedelta(minutes=2), force=True)

    assert first is second
    assert first.next_allowed_at == NOW + timedelta(minutes=5)
    assert calls == 1


def test_http_429_updates_cooldown_and_blocks_followup_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    next_allowed_at = NOW + timedelta(minutes=10)

    def urlopen(_request: object, timeout: float) -> FakeHttpResponse:
        nonlocal calls
        calls += 1
        assert timeout == 15
        if calls == 1:
            return FakeHttpResponse(json.dumps(response()).encode())
        body = json.dumps(
            {
                "error": "rate limited",
                "rateLimit": {"nextAllowedAt": int(next_allowed_at.timestamp() * 1_000)},
            }
        ).encode()
        raise HTTPError(
            "https://perptape.com/api/v1/breakouts",
            429,
            "rate limited",
            None,
            BytesIO(body),
        )

    monkeypatch.setattr(perptape_module.urllib.request, "urlopen", urlopen)
    client = PerptapeClient(
        base_url="https://perptape.com",
        api_key="key",
        contract_version="breakouts-v1",
        cache_ttl=timedelta(minutes=1),
    )
    client.refresh(now=NOW)
    assert client.remote_request_state() == (1, 1, 0, 0)

    with pytest.raises(PerptapeRateLimited) as limited:
        client.refresh(now=NOW + timedelta(minutes=2), force=True)
    state_after_remote_429 = client.remote_request_state()
    with pytest.raises(PerptapeRateLimited) as locally_limited:
        client.refresh(now=NOW + timedelta(minutes=3), force=True)

    assert limited.value.next_allowed_at == next_allowed_at
    assert limited.value.is_remote is True
    assert locally_limited.value.next_allowed_at == next_allowed_at
    assert locally_limited.value.is_remote is False
    assert state_after_remote_429 == (2, 1, 1, 1)
    assert client.remote_request_state() == state_after_remote_429
    assert calls == 2


def test_invalid_or_rejected_response_does_not_forge_remote_success() -> None:
    values: list[dict[str, object] | DomainRejected] = [
        {"type": "wrong", "data": []},
        DomainRejected("PERPTAPE_UNAVAILABLE", "unavailable"),
    ]

    def fetch(_url: str, _headers: dict[str, str], _timeout: float) -> dict[str, object]:
        value = values.pop(0)
        if isinstance(value, DomainRejected):
            raise value
        return value

    client = PerptapeClient(
        base_url="https://perptape.com",
        api_key="key",
        contract_version="breakouts-v1",
        cache_ttl=timedelta(seconds=0),
        fetcher=fetch,
    )

    with pytest.raises(DomainRejected, match="PERPTAPE_RESPONSE_INVALID"):
        client.refresh(now=NOW, force=True)
    assert client.remote_request_state() == (0, 0, 0, 0)

    with pytest.raises(DomainRejected, match="PERPTAPE_UNAVAILABLE"):
        client.refresh(now=NOW + timedelta(seconds=1), force=True)
    assert client.remote_request_state() == (0, 0, 0, 0)


def test_remote_request_state_is_shared_and_serialized_between_callers() -> None:
    calls = 0

    def fetch(_url: str, _headers: dict[str, str], _timeout: float) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return response()

    client = PerptapeClient(
        base_url="https://perptape.com",
        api_key="key",
        contract_version="breakouts-v1",
        cache_ttl=timedelta(seconds=0),
        fetcher=fetch,
    )
    errors: list[BaseException] = []

    def refresh() -> None:
        try:
            client.refresh(now=NOW + timedelta(seconds=1), force=True)
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=refresh) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=1)

    assert errors == []
    assert all(not thread.is_alive() for thread in threads)
    assert calls == 2
    assert client.remote_request_state() == (2, 2, 0, 0)


def test_client_fails_closed_without_api_key_or_with_invalid_contract() -> None:
    missing = PerptapeClient(
        base_url="https://perptape.com",
        api_key=None,
        contract_version="breakouts-v1",
        cache_ttl=timedelta(minutes=1),
    )
    with pytest.raises(DomainRejected, match="PERPTAPE_NOT_CONFIGURED"):
        missing.list_candidates(now=NOW)

    invalid = PerptapeClient(
        base_url="https://perptape.com",
        api_key="key",
        contract_version="breakouts-v1",
        cache_ttl=timedelta(minutes=1),
        fetcher=lambda _url, _headers, _timeout: {"type": "wrong", "data": []},
    )
    with pytest.raises(DomainRejected, match="PERPTAPE_RESPONSE_INVALID"):
        invalid.list_candidates(now=NOW)


@pytest.mark.parametrize(
    "base_url",
    [
        "http://perptape.com",
        "https://perptape.example",
        "https://perptape.com.evil.example",
        "https://perptape.com/api/v1",
        "https://user@perptape.com",
    ],
)
def test_client_rejects_nonofficial_https_hosts_before_network(base_url: str) -> None:
    with pytest.raises(ValueError, match="official HTTPS host"):
        PerptapeClient(
            base_url=base_url,
            api_key="key",
            contract_version="breakouts-v1",
            cache_ttl=timedelta(minutes=1),
        )


def test_forced_refresh_bypasses_the_normal_snapshot_cache_for_gap_recovery() -> None:
    calls = 0

    def fetch(_url: str, _headers: dict[str, str], _timeout: float) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return response()

    client = PerptapeClient(
        base_url="https://perptape.com",
        api_key="key",
        contract_version="breakouts-v1",
        cache_ttl=timedelta(minutes=5),
        fetcher=fetch,
    )

    client.refresh(now=NOW)
    client.refresh(now=NOW + timedelta(seconds=1), force=True)

    assert calls == 2


def test_candidate_lookup_does_not_invent_missing_source_fact() -> None:
    client = PerptapeClient(
        base_url="https://perptape.com",
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
        base_url="https://perptape.com",
        api_key="key",
        contract_version="breakouts-v1",
        cache_ttl=timedelta(minutes=1),
        fetcher=lambda _url, _headers, _timeout: invalid_response,
    )

    with pytest.raises(DomainRejected, match="PERPTAPE_RESPONSE_INVALID"):
        client.list_candidates(now=NOW)


def test_snapshot_identity_is_canonical_for_order_timezone_decimal_and_defaults() -> None:
    feed = replace(
        parsed_feed(),
        candidates=tuple(
            replace(candidate, chart_url="") for candidate in parsed_feed().candidates
        ),
    )
    offset = timezone(timedelta(hours=8))
    equivalent = replace(
        feed,
        generated_at=feed.generated_at.astimezone(offset),
        fetched_at=feed.fetched_at.astimezone(offset),
        next_allowed_at=feed.next_allowed_at.astimezone(offset),
        candidates=tuple(
            replace(
                candidate,
                observed_at=candidate.observed_at.astimezone(offset),
                triggered_at=(
                    None
                    if candidate.triggered_at is None
                    else candidate.triggered_at.astimezone(offset)
                ),
                reference_price=Decimal(f"{candidate.reference_price}.00"),
                threshold=(
                    None if candidate.threshold is None else Decimal(f"{candidate.threshold}.000")
                ),
                chart_url=PerptapeCandidate.from_dict(
                    {key: value for key, value in candidate.to_dict().items() if key != "chart_url"}
                ).chart_url,
            )
            for candidate in reversed(feed.candidates)
        ),
    )

    assert perptape_snapshot_identity(equivalent) == perptape_snapshot_identity(feed)
    changed = replace(
        equivalent,
        candidates=(
            replace(equivalent.candidates[0], rationale="materially changed"),
            *equivalent.candidates[1:],
        ),
    )
    assert perptape_snapshot_identity(changed) != perptape_snapshot_identity(feed)


EXTREME_POSITIVE_OFFSET = timezone(timedelta(hours=23, minutes=59))
EXTREME_NEGATIVE_OFFSET = timezone(-timedelta(hours=23, minutes=59))
INVALID_PERPTAPE_TIMES = [
    pytest.param(NOW.replace(tzinfo=None), id="naive"),
    pytest.param(datetime.min.replace(tzinfo=EXTREME_POSITIVE_OFFSET), id="min-underflow"),
    pytest.param(datetime.max.replace(tzinfo=EXTREME_NEGATIVE_OFFSET), id="max-overflow"),
]


@pytest.mark.parametrize("invalid_time", INVALID_PERPTAPE_TIMES)
@pytest.mark.parametrize(
    "field",
    ["generated_at", "fetched_at", "next_allowed_at", "observed_at", "triggered_at"],
)
def test_snapshot_rejects_unusable_datetime_before_identity_or_sort(
    field: str,
    invalid_time: datetime,
) -> None:
    feed = parsed_feed()
    if field in {"observed_at", "triggered_at"}:
        candidate = replace(feed.candidates[0], **{field: invalid_time})
        invalid = replace(feed, candidates=(candidate, *feed.candidates[1:]))
    else:
        invalid = replace(feed, **{field: invalid_time})

    with pytest.raises(DomainRejected, match="PERPTAPE_DATETIME_INVALID"):
        perptape_snapshot_identity(invalid)
    with pytest.raises(DomainRejected, match="PERPTAPE_DATETIME_INVALID"):
        bound_perptape_feed_snapshot(invalid)


@pytest.mark.parametrize(
    "invalid_time",
    [
        *INVALID_PERPTAPE_TIMES,
        pytest.param(datetime.max.replace(tzinfo=UTC), id="max-no-operational-headroom"),
    ],
)
def test_client_rejects_invalid_clock_before_any_remote_request(
    invalid_time: datetime,
) -> None:
    calls = 0

    def fetch(_url: str, _headers: dict[str, str], _timeout: float) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return response()

    client = PerptapeClient(
        base_url="https://perptape.com",
        api_key="key",
        contract_version="breakouts-v1",
        cache_ttl=timedelta(0),
        fetcher=fetch,
    )

    with pytest.raises(DomainRejected, match="PERPTAPE_DATETIME_INVALID"):
        client.refresh(now=invalid_time)
    assert calls == 0


def test_datetime_utc_normalization_accepts_exact_edges_and_dst_folds() -> None:
    minimum_boundary = (datetime.min + timedelta(hours=23, minutes=59)).replace(
        tzinfo=EXTREME_POSITIVE_OFFSET
    )
    maximum_boundary = (datetime.max - timedelta(hours=23, minutes=59)).replace(
        tzinfo=EXTREME_NEGATIVE_OFFSET
    )
    assert normalize_perptape_datetime(minimum_boundary) == datetime.min.replace(tzinfo=UTC)
    assert normalize_perptape_datetime(maximum_boundary) == datetime.max.replace(tzinfo=UTC)
    with pytest.raises(DomainRejected, match="PERPTAPE_DATETIME_INVALID"):
        normalize_perptape_datetime(minimum_boundary - timedelta(microseconds=1))
    with pytest.raises(DomainRejected, match="PERPTAPE_DATETIME_INVALID"):
        normalize_perptape_datetime(maximum_boundary + timedelta(microseconds=1))

    new_york = ZoneInfo("America/New_York")
    first_fold = datetime(2026, 11, 1, 1, 30, fold=0, tzinfo=new_york)
    second_fold = datetime(2026, 11, 1, 1, 30, fold=1, tzinfo=new_york)
    assert normalize_perptape_datetime(first_fold) == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    assert normalize_perptape_datetime(second_fold) == datetime(2026, 11, 1, 6, 30, tzinfo=UTC)


def _feed_with_reference_price(value: Decimal) -> PerptapeFeedSnapshot:
    feed = parsed_feed()
    return replace(
        feed,
        candidates=(replace(feed.candidates[0], reference_price=value),),
    )


@pytest.mark.parametrize(
    "value",
    [
        Decimal("1E999999999999999999"),
        Decimal("1E-999999999999999999"),
        Decimal("NaN"),
        Decimal("sNaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
        Decimal("-0"),
    ],
)
def test_decimal_extremes_fail_before_canonical_string_expansion(value: Decimal) -> None:
    with pytest.raises(DomainRejected, match="PERPTAPE_DECIMAL_INVALID"):
        perptape_snapshot_identity(_feed_with_reference_price(value))


@pytest.mark.parametrize("precision_delta", [-1, 0, 1])
def test_decimal_precision_boundary(precision_delta: int) -> None:
    precision = PERPTAPE_DECIMAL_MAX_PRECISION + precision_delta
    integer_digits = PERPTAPE_DECIMAL_MAX_ADJUSTED_EXPONENT + 1
    fractional_digits = precision - integer_digits
    value = Decimal((0, (1,) * precision, -fractional_digits))

    if precision_delta <= 0:
        perptape_snapshot_identity(_feed_with_reference_price(value))
    else:
        with pytest.raises(DomainRejected, match="PERPTAPE_DECIMAL_INVALID"):
            perptape_snapshot_identity(_feed_with_reference_price(value))


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        (Decimal(f"1E{PERPTAPE_DECIMAL_MAX_ADJUSTED_EXPONENT - 1}"), True),
        (Decimal(f"1E{PERPTAPE_DECIMAL_MAX_ADJUSTED_EXPONENT}"), True),
        (Decimal(f"1E{PERPTAPE_DECIMAL_MAX_ADJUSTED_EXPONENT + 1}"), False),
        (Decimal(f"1E{PERPTAPE_DECIMAL_MIN_ADJUSTED_EXPONENT + 1}"), True),
        (Decimal(f"1E{PERPTAPE_DECIMAL_MIN_ADJUSTED_EXPONENT}"), True),
        (Decimal(f"1E{PERPTAPE_DECIMAL_MIN_ADJUSTED_EXPONENT - 1}"), False),
    ],
)
def test_decimal_adjusted_exponent_boundaries(value: Decimal, valid: bool) -> None:
    if valid:
        perptape_snapshot_identity(_feed_with_reference_price(value))
    else:
        with pytest.raises(DomainRejected, match="PERPTAPE_DECIMAL_INVALID"):
            perptape_snapshot_identity(_feed_with_reference_price(value))


@pytest.mark.parametrize("canonical_delta", [-1, 0, 1])
def test_decimal_fixed_expansion_boundary(canonical_delta: int) -> None:
    precision = 21 + canonical_delta
    exponent = PERPTAPE_DECIMAL_MIN_TUPLE_EXPONENT - canonical_delta
    value = Decimal((0, (1,) * precision, exponent))
    expected_bytes = 2 - exponent
    assert expected_bytes == PERPTAPE_DECIMAL_MAX_CANONICAL_BYTES + canonical_delta

    if canonical_delta <= 0:
        perptape_snapshot_identity(_feed_with_reference_price(value))
    else:
        with pytest.raises(DomainRejected, match="PERPTAPE_DECIMAL_INVALID"):
            perptape_snapshot_identity(_feed_with_reference_price(value))


def test_http_decimal_raw_byte_ceiling_rejects_before_snapshot_state_changes() -> None:
    payload = response()
    data = payload["data"]
    assert isinstance(data, list)
    first = data[0]
    assert isinstance(first, dict)
    first["price"] = "1" * 129
    client = PerptapeClient(
        base_url="https://perptape.com",
        api_key="key",
        contract_version="breakouts-v1",
        cache_ttl=timedelta(0),
        fetcher=lambda _url, _headers, _timeout: payload,
    )

    with pytest.raises(DomainRejected, match="PERPTAPE_DECIMAL_INVALID"):
        client.refresh(now=NOW)
    assert client.remote_request_state() == (0, 0, 0, 0)


def test_candidate_window_selection_is_independent_of_input_order() -> None:
    candidate = parsed_feed().candidates[0]
    candidates = tuple(
        replace(
            candidate,
            candidate_id=f"pt_window_{index}",
            symbol=f"W{index}USDT",
            canonical_symbol=f"W{index}",
            observed_at=NOW + timedelta(microseconds=index),
            triggered_at=NOW + timedelta(microseconds=index),
        )
        for index in range(PERPTAPE_CANDIDATE_WINDOW + 2)
    )
    forward = bound_perptape_feed_snapshot(replace(parsed_feed(), candidates=candidates)).candidates
    reverse = bound_perptape_feed_snapshot(
        replace(parsed_feed(), candidates=tuple(reversed(candidates)))
    ).candidates

    assert forward == reverse
    assert len(forward) == PERPTAPE_CANDIDATE_WINDOW
    assert forward[0].symbol == "W2USDT"
    assert forward[-1].symbol == f"W{PERPTAPE_CANDIDATE_WINDOW + 1}USDT"


@pytest.mark.parametrize("byte_delta", [-1, 0, 1])
def test_http_symbol_length_uses_utf8_byte_boundaries(byte_delta: int) -> None:
    payload = response()
    data = payload["data"]
    assert isinstance(data, list)
    first = data[0]
    assert isinstance(first, dict)
    limit = PERPTAPE_STRING_FIELD_MAX_BYTES["symbol"]
    first["symbol"] = "S" * (limit + byte_delta)
    client = PerptapeClient(
        base_url="https://perptape.com",
        api_key="key",
        contract_version="breakouts-v1",
        cache_ttl=timedelta(0),
        fetcher=lambda _url, _headers, _timeout: payload,
    )

    if byte_delta <= 0:
        assert client.refresh(now=NOW).candidates[0].symbol == first["symbol"]
    else:
        with pytest.raises(DomainRejected, match="PERPTAPE_FIELD_TOO_LARGE"):
            client.refresh(now=NOW)


def test_http_symbol_rejects_multibyte_value_over_utf8_ceiling() -> None:
    payload = response()
    data = payload["data"]
    assert isinstance(data, list)
    first = data[0]
    assert isinstance(first, dict)
    first["symbol"] = "界" * 21
    client = PerptapeClient(
        base_url="https://perptape.com",
        api_key="key",
        contract_version="breakouts-v1",
        cache_ttl=timedelta(0),
        fetcher=lambda _url, _headers, _timeout: payload,
    )
    assert client.refresh(now=NOW).candidates[0].symbol == "界" * 21

    first["symbol"] = "界" * 22
    with pytest.raises(DomainRejected, match="PERPTAPE_FIELD_TOO_LARGE"):
        client.refresh(now=NOW + timedelta(seconds=1), force=True)


@pytest.mark.parametrize("field", ["candidate_id", "detail_url", "chart_url"])
def test_persisted_id_and_url_fields_enforce_byte_ceiling(field: str) -> None:
    candidate = parsed_feed().candidates[0]
    limit = PERPTAPE_STRING_FIELD_MAX_BYTES[field]
    for delta in (-1, 0):
        value = candidate.to_dict()
        value[field] = "x" * (limit + delta)
        assert getattr(PerptapeCandidate.from_dict(value), field) == value[field]
    value = candidate.to_dict()
    value[field] = "x" * (limit + 1)
    with pytest.raises(DomainRejected, match="PERPTAPE_FIELD_TOO_LARGE"):
        PerptapeCandidate.from_dict(value)


def _max_payload_candidate(
    template: PerptapeCandidate,
    index: int,
) -> PerptapeCandidate:
    candidate_prefix = f"id{index}:"
    symbol_prefix = f"S{index}:"
    return replace(
        template,
        candidate_id=candidate_prefix
        + "i" * (PERPTAPE_STRING_FIELD_MAX_BYTES["candidate_id"] - len(candidate_prefix)),
        symbol=symbol_prefix
        + "s" * (PERPTAPE_STRING_FIELD_MAX_BYTES["symbol"] - len(symbol_prefix)),
        canonical_symbol=symbol_prefix
        + "c" * (PERPTAPE_STRING_FIELD_MAX_BYTES["canonical_symbol"] - len(symbol_prefix)),
        observed_at=NOW + timedelta(microseconds=index),
        triggered_at=NOW + timedelta(microseconds=index),
        rationale="r" * PERPTAPE_STRING_FIELD_MAX_BYTES["rationale"],
        detail_url="d" * PERPTAPE_STRING_FIELD_MAX_BYTES["detail_url"],
        chart_url="c" * PERPTAPE_STRING_FIELD_MAX_BYTES["chart_url"],
    )


def _feed_at_payload_ceiling() -> PerptapeFeedSnapshot:
    template = parsed_feed().candidates[0]
    candidates = tuple(
        _max_payload_candidate(template, index) for index in range(PERPTAPE_CANDIDATE_WINDOW)
    )
    low = 1
    high = len(candidates)
    while low < high:
        middle = (low + high) // 2
        size = perptape_payload_size_bytes(replace(parsed_feed(), candidates=candidates[:middle]))
        if size >= PERPTAPE_PAYLOAD_MAX_BYTES:
            high = middle
        else:
            low = middle + 1
    selected = list(candidates[:low])
    feed = replace(parsed_feed(), candidates=tuple(selected))
    excess = perptape_payload_size_bytes(feed) - PERPTAPE_PAYLOAD_MAX_BYTES
    assert excess >= 0
    fields = (
        ("chart_url", ""),
        ("detail_url", "d"),
        ("rationale", "r"),
        ("canonical_symbol", "C"),
        ("symbol", "S"),
    )
    for index in range(len(selected) - 1, -1, -1):
        candidate = selected[index]
        for field, minimum in fields:
            value = getattr(candidate, field)
            removable = len(value) - len(minimum)
            removed = min(excess, removable)
            if removed:
                candidate = replace(candidate, **{field: value[: len(value) - removed]})
                excess -= removed
            if excess == 0:
                break
        selected[index] = candidate
        if excess == 0:
            break
    assert excess == 0
    feed = replace(feed, candidates=tuple(selected))
    assert perptape_payload_size_bytes(feed) == PERPTAPE_PAYLOAD_MAX_BYTES
    return feed


def test_normalized_payload_byte_ceiling_accepts_boundary_and_rejects_plus_one() -> None:
    exact = _feed_at_payload_ceiling()
    adjustable_index = next(
        index
        for index, candidate in enumerate(exact.candidates)
        if len(candidate.chart_url) < PERPTAPE_STRING_FIELD_MAX_BYTES["chart_url"]
    )
    adjustable = exact.candidates[adjustable_index]
    below_candidates = list(exact.candidates)
    below_candidates[0] = replace(
        below_candidates[0],
        chart_url=below_candidates[0].chart_url[:-1],
    )
    above_candidates = list(exact.candidates)
    above_candidates[adjustable_index] = replace(
        adjustable,
        chart_url=adjustable.chart_url + "x",
    )
    below = replace(exact, candidates=tuple(below_candidates))
    above = replace(exact, candidates=tuple(above_candidates))

    assert perptape_payload_size_bytes(below) == PERPTAPE_PAYLOAD_MAX_BYTES - 1
    assert perptape_payload_size_bytes(exact) == PERPTAPE_PAYLOAD_MAX_BYTES
    assert perptape_payload_size_bytes(above) == PERPTAPE_PAYLOAD_MAX_BYTES + 1
    validate_perptape_feed_payload(below)
    validate_perptape_feed_payload(exact)
    with pytest.raises(DomainRejected, match="PERPTAPE_PAYLOAD_TOO_LARGE"):
        validate_perptape_feed_payload(above)


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
        base_url="https://perptape.com",
        api_key="key",
        contract_version="breakouts-v1",
        cache_ttl=timedelta(minutes=1),
    )
    assert client.list_candidates(now=NOW) == []

    def denied(_request: object, timeout: float) -> FakeHttpResponse:
        raise HTTPError("https://perptape.com", 403, "denied", None, None)

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
