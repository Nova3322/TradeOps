from __future__ import annotations

import json
from uuid import UUID

import pytest

from trading_control_plane import domain
from trading_control_plane.freqtrade_provision import (
    CONTROL_PLANE_TIMEFRAME,
    HYPERLIQUID_READ_RATE_LIMIT_MS,
    WORKERS,
    _discover_hyperliquid_hip3_dexes,
    _runtime_config,
    _worker_environment,
)
from trading_control_plane.runtime_contracts import PreparedFreqtradeWorkerBinding


def _binding(venue: str) -> PreparedFreqtradeWorkerBinding:
    return PreparedFreqtradeWorkerBinding(
        exchange_account_id=UUID("00000000-0000-0000-0000-000000000001"),
        workspace_id=UUID("00000000-0000-0000-0000-000000000002"),
        team_id=UUID("00000000-0000-0000-0000-000000000003"),
        account_id="exact-live-account",
        venue=venue,
        environment="LIVE",
        account_version=7,
        worker_name=f"freqtrade-{venue.lower()}-live-00000000",
        worker_url=f"http://freqtrade-{venue.lower()}-live:8080",
        worker_mode="LIVE",
        worker_status="NOT_VERIFIED",
        auth_version=1,
        username="generated-control-user",
        password="generated-control-password",  # noqa: S106
        hip3_dexes=(),
        ws_token="generated-websocket-token",  # noqa: S106
        service_principal_id=UUID("00000000-0000-0000-0000-000000000004"),
    )


def _template(venue: str) -> dict[str, object]:
    return {
        "dry_run": True,
        "initial_state": "stopped",
        "force_entry_enable": False,
        "position_adjustment_enable": True,
        "cancel_open_orders_on_exit": False,
        "exchange": {
            "name": venue.lower(),
            "ccxt_config": {"enableRateLimit": True},
            "ccxt_async_config": {"enableRateLimit": True},
            "pair_whitelist": ["CANARY/USDT:USDT"],
            "pair_blacklist": [],
            "hip3_dexes": ["xyz"],
        },
        "api_server": {"enabled": True},
        "telegram": {"enabled": False},
    }


def test_runtime_configs_enable_exact_live_workers_with_full_official_pair_scope() -> None:
    hip3_dexes = ("xyz", "flx")
    for venue, definition in WORKERS.items():
        payload = json.loads(
            _runtime_config(
                _template(venue),
                definition,
                "account-1",
                hip3_dexes=hip3_dexes,
            )
        )

        assert payload["dry_run"] is False
        assert payload["initial_state"] == "running"
        assert payload["force_entry_enable"] is True
        assert payload["max_open_trades"] == -1
        assert payload["position_adjustment_enable"] is True
        assert payload["cancel_open_orders_on_exit"] is False
        assert payload["timeframe"] == CONTROL_PLANE_TIMEFRAME
        assert payload["exchange"]["pair_whitelist"] == [definition.pair_pattern]
        assert payload["exchange"]["pair_blacklist"] == []
        assert payload["exchange"]["enable_ws"] is False
        assert payload["api_server"]["enable_openapi"] is False
        assert payload["telegram"]["enabled"] is False
        if venue == "HYPERLIQUID":
            assert payload["exchange"]["hip3_dexes"] == list(hip3_dexes)
            for key in ("ccxt_config", "ccxt_async_config"):
                assert payload["exchange"][key]["enableRateLimit"] is True
                assert (
                    payload["exchange"][key]["rateLimit"]
                    == HYPERLIQUID_READ_RATE_LIMIT_MS
                )
                assert payload["exchange"][key]["options"] == {
                    "defaultType": "swap",
                    "fetchMarkets": {
                        "types": ["swap", "hip3"],
                        "hip3": {
                            "dexes": list(hip3_dexes),
                            "limit": len(hip3_dexes),
                        },
                    },
                }


def test_official_hip3_directory_discovers_every_active_usdc_dex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response:
        def __init__(self, payload: object) -> None:
            self.status_code = 200
            self.payload = payload

        def json(self) -> object:
            return self.payload

    def post(_url: str, *, json: dict[str, object], **_kwargs: object) -> Response:
        if json == {"type": "perpDexs"}:
            return Response(
                [
                    None,
                    {"name": "xyz"},
                    {"name": "flx"},
                    {"name": "para"},
                    {"name": "abcd"},
                ]
            )
        metadata = {
            "xyz": {
                "collateralToken": 0,
                "universe": [{"name": "xyz:TSLA"}],
            },
            "flx": {
                "collateralToken": 360,
                "universe": [{"name": "flx:GOLD"}],
            },
            "para": {
                "collateralToken": 0,
                "universe": [{"name": "para:TOTAL2"}],
            },
            "abcd": {
                "collateralToken": 0,
                "universe": [{"name": "abcd:USA500", "isDelisted": True}],
            },
        }
        return Response(metadata[str(json["dex"])])

    monkeypatch.setattr(
        "trading_control_plane.freqtrade_provision.requests.post",
        post,
    )

    assert _discover_hyperliquid_hip3_dexes() == ("xyz", "para")


def test_official_hip3_directory_fails_closed_on_ambiguous_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response:
        status_code = 200

        @staticmethod
        def json() -> list[dict[str, object] | None]:
            return [None, {"name": "xyz"}, {"unexpected": "flx"}]

    monkeypatch.setattr(
        "trading_control_plane.freqtrade_provision.requests.post",
        lambda *_args, **_kwargs: Response(),
    )

    with pytest.raises(domain.DomainRejected, match="FREQTRADE_HIP3_DIRECTORY_INVALID"):
        _discover_hyperliquid_hip3_dexes()


def test_worker_environment_uses_generated_internal_auth_and_exact_encrypted_credentials() -> None:
    binance = _worker_environment(
        WORKERS["BINANCE"],
        _binding("BINANCE"),
        {"api_key": "fixture-key", "api_secret": "fixture-secret"},
    )
    hyperliquid = _worker_environment(
        WORKERS["HYPERLIQUID"],
        _binding("HYPERLIQUID"),
        {
            "account_address": "0x1111111111111111111111111111111111111111",
            "api_wallet_address": "0x2222222222222222222222222222222222222222",
            "api_wallet_private_key": "fixture-private-key",
        },
    )

    assert 'FREQTRADE__DRY_RUN="false"' in binance
    assert 'FREQTRADE__FORCE_ENTRY_ENABLE="true"' in binance
    assert 'FREQTRADE__EXCHANGE__KEY="fixture-key"' in binance
    assert 'FREQTRADE__EXCHANGE__SECRET="fixture-secret"' in binance
    assert 'FREQTRADE__EXCHANGE__WALLET_ADDRESS=' not in binance
    assert (
        'FREQTRADE__EXCHANGE__WALLET_ADDRESS="0x1111111111111111111111111111111111111111"'
        in hyperliquid
    )
    assert 'FREQTRADE__EXCHANGE__PRIVATE_KEY="fixture-private-key"' in hyperliquid
    assert "api_wallet_address" not in hyperliquid
