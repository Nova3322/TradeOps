from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from trading_control_plane.binance import (
    BinancePortfolioMarginReadOnlyClient,
    BinanceReadOnlyClient,
)
from trading_control_plane.config import get_settings
from trading_control_plane.domain import DomainRejected
from trading_control_plane.hyperliquid import (
    HyperliquidReadOnlyClient,
    resolve_hyperliquid_main_account,
)
from trading_control_plane.notilt import NoTiltGateway
from trading_control_plane.perptape import PerptapeClient


def _probe(action: Any) -> dict[str, Any]:
    try:
        count = int(action())
    except DomainRejected as exc:
        return {"status": "FAILED", "error_code": exc.code}
    except (OSError, TimeoutError, ValueError):
        return {"status": "FAILED", "error_code": "READ_ONLY_PROBE_FAILED"}
    return {"status": "SUCCESS", "items_observed": count}


def _probe_notilt_assignment(gateway: NoTiltGateway, chain_id: int, agent: str) -> int:
    _vault, active = gateway.resolve_assignment(chain_id, agent)
    if not active:
        raise DomainRejected(
            "NOTILT_ASSIGNMENT_INACTIVE",
            "NoTilt Registry assignment is not active",
        )
    return 1


def main() -> int:
    settings = get_settings()
    now = datetime.now(UTC)
    results: dict[str, dict[str, Any]] = {}

    if settings.perptape_api_key:
        client = PerptapeClient(
            base_url=settings.perptape_base_url,
            api_key=settings.perptape_api_key,
            contract_version=settings.perptape_contract_version,
            cache_ttl=timedelta(seconds=settings.perptape_cache_seconds),
            timeout_seconds=settings.perptape_timeout_seconds,
        )
        results["PERPTAPE"] = _probe(lambda: len(client.refresh(now=now).candidates))
    else:
        results["PERPTAPE"] = {
            "status": "SKIPPED",
            "error_code": "PERPTAPE_CREDENTIALS_NOT_LOADED",
        }

    if settings.binance_read_only_enabled:
        binance = (
            BinancePortfolioMarginReadOnlyClient(
                base_url=settings.binance_live_base_url,
                api_key=settings.binance_api_key,
                api_secret=settings.binance_api_secret,
                recv_window_ms=settings.binance_recv_window_ms,
            )
            if settings.binance_account_mode == "PORTFOLIO_MARGIN"
            else BinanceReadOnlyClient(
                base_url=settings.binance_futures_base_url,
                api_key=settings.binance_api_key,
                api_secret=settings.binance_api_secret,
                recv_window_ms=settings.binance_recv_window_ms,
            )
        )
        results["BINANCE"] = _probe(
            lambda: len(
                binance.read_account_snapshots(
                    (settings.runtime_binance_symbol,),
                    now=now,
                )
            )
        )
    else:
        results["BINANCE"] = {
            "status": "SKIPPED",
            "error_code": "BINANCE_READ_ONLY_DISABLED",
        }

    if settings.hyperliquid_read_only_enabled:

        def probe_hyperliquid() -> int:
            account = resolve_hyperliquid_main_account(
                base_url=settings.hyperliquid_base_url,
                account_address=settings.hyperliquid_account_address,
                api_wallet_address=settings.hyperliquid_api_wallet_address,
            )
            client = HyperliquidReadOnlyClient(
                base_url=settings.hyperliquid_base_url,
                account_address=settings.hyperliquid_subaccount_address or account,
                dex=settings.hyperliquid_core_dex,
                hip3_dexes=settings.hyperliquid_hip3_dexes,
            )
            return len(
                client.read_account_snapshots(
                    (settings.runtime_hyperliquid_symbol,),
                    now=now,
                )
            )

        results["HYPERLIQUID"] = _probe(probe_hyperliquid)
    else:
        results["HYPERLIQUID"] = {
            "status": "SKIPPED",
            "error_code": "HYPERLIQUID_READ_ONLY_DISABLED",
        }

    notilt = NoTiltGateway(timeout_seconds=settings.notilt_gateway_timeout_seconds)
    if not settings.notilt_enabled:
        results["NOTILT"] = {
            "status": "SKIPPED",
            "error_code": "NOTILT_READ_ONLY_DISABLED",
        }
    elif not settings.notilt_vaults:
        results["NOTILT"] = {
            "status": "SKIPPED",
            "error_code": "NOTILT_VAULT_SCOPE_MISSING",
            "gateway_runtime_available": notilt.available,
            "registry_assignments": {
                str(chain_id): _probe(
                    lambda chain_id=chain_id: _probe_notilt_assignment(
                        notilt,
                        chain_id,
                        settings.notilt_agent_address or "",
                    )
                )
                for chain_id in (1, 56, 42161)
            },
        }
    else:
        for chain_id, vault in sorted(settings.notilt_vaults.items()):
            results[f"NOTILT:{chain_id}"] = _probe(
                lambda chain_id=chain_id, vault=vault: len(
                    notilt.read_vault(
                        chain_id,
                        vault,
                        settings.notilt_agent_address or "",
                    ).budgets
                )
            )

    output = {
        "mode": "READ_ONLY_NO_SIDE_EFFECTS",
        "dangerous_process_gates": {
            "binance_live_order_send": settings.binance_live_order_send_enabled,
            "binance_testnet_order_send": settings.binance_testnet_order_send_enabled,
            "hyperliquid_live_order_send": settings.hyperliquid_live_order_send_enabled,
            "hyperliquid_testnet_order_send": (settings.hyperliquid_testnet_order_send_enabled),
        },
        "sources": results,
    }
    print(json.dumps(output, separators=(",", ":"), sort_keys=True))
    return 0 if all(item["status"] == "SUCCESS" for item in results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
