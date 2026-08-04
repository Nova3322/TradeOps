from __future__ import annotations

import argparse
import os
import shutil
import subprocess

from trading_control_plane.config import get_settings
from trading_control_plane.domain import DomainRejected
from trading_control_plane.hyperliquid import resolve_hyperliquid_main_account


def _resolve_main_account() -> str:
    settings = get_settings()
    if settings.hyperliquid_fact_environment != "LIVE":
        raise SystemExit("Hyperliquid live smoke requires LIVE fact environment")
    if not settings.hyperliquid_api_wallet_private_key:
        raise SystemExit("Hyperliquid API wallet private key is not loaded")
    account = resolve_hyperliquid_main_account(
        base_url=settings.hyperliquid_base_url,
        account_address=settings.hyperliquid_account_address,
        api_wallet_address=settings.hyperliquid_api_wallet_address,
    )
    if account is None:
        raise SystemExit("Hyperliquid main account could not be resolved")
    return account


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Start the isolated Freqtrade Hyperliquid smoke worker without printing secrets"
    )
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--acknowledge-real-order", action="store_true")
    args = parser.parse_args()
    if args.live != args.acknowledge_real_order:
        raise SystemExit("real mode requires both --live and --acknowledge-real-order")

    settings = get_settings()
    try:
        account = _resolve_main_account()
    except DomainRejected as exc:
        raise SystemExit(f"Hyperliquid main account resolution failed: {exc.code}") from exc
    environment = os.environ.copy()
    environment.update(
        {
            "TRADING_HYPERLIQUID_FREQTRADE_WALLET_ADDRESS": account,
            "TRADING_HYPERLIQUID_API_WALLET_PRIVATE_KEY": (
                settings.hyperliquid_api_wallet_private_key or ""
            ),
            "TRADING_FREQTRADE_API_USERNAME": "trading-control",
            "TRADING_FREQTRADE_API_PASSWORD": "local-live-smoke-only",
            "TRADING_FREQTRADE_LIVE_JWT_SECRET": "local-live-smoke-jwt-runtime-only",
            "TRADING_FREQTRADE_HYPERLIQUID_LIVE_DRY_RUN": (
                "false" if args.live else "true"
            ),
            "TRADING_FREQTRADE_HYPERLIQUID_FORCE_ENTRY_ENABLE": (
                "true" if args.live else "false"
            ),
            "TRADING_FREQTRADE_HYPERLIQUID_INITIAL_STATE": (
                "running" if args.live else "stopped"
            ),
        }
    )
    docker = shutil.which("docker")
    if docker is None:
        raise SystemExit("docker executable is unavailable")
    completed = subprocess.run(  # noqa: S603 - fixed argv, trusted resolved docker binary
        [
            docker,
            "compose",
            "--profile",
            "live-smoke",
            "up",
            "-d",
            "freqtrade-hyperliquid-live-smoke",
        ],
        check=False,
        env=environment,
    )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
