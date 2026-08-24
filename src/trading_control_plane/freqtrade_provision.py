from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import secrets
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

import requests
from sqlalchemy import select

from trading_control_plane import domain, models, rejections
from trading_control_plane.config import get_settings
from trading_control_plane.database import Database
from trading_control_plane.freqtrade import FreqtradeWorkerClient, FreqtradeWorkerSpec
from trading_control_plane.freqtrade_contracts import freqtrade_pair, parse_hip3_dexes
from trading_control_plane.runtime_contracts import PreparedFreqtradeWorkerBinding
from trading_control_plane.service import TradingService

FREQTRADE_UID = 1000
FREQTRADE_GID = 1000
SUPPORTED_VENUES = ("BINANCE", "HYPERLIQUID")
CONTROL_PLANE_TIMEFRAME = "1h"
HYPERLIQUID_READ_RATE_LIMIT_MS = 1500
HYPERLIQUID_INFO_URL = "https://api.hyperliquid.xyz/info"
HYPERLIQUID_HIP3_DEX_LIMIT = 128


@dataclass(frozen=True, slots=True)
class WorkerDefinition:
    venue: Literal["BINANCE", "HYPERLIQUID"]
    service_name: str
    template_name: str
    env_name: str
    config_name: str
    pair_pattern: str

    def worker_name(self, exchange_account_id: UUID) -> str:
        return f"{self.service_name}-{exchange_account_id.hex[:8]}"

    @property
    def worker_url(self) -> str:
        return f"http://{self.service_name}:8080"


WORKERS = {
    "BINANCE": WorkerDefinition(
        venue="BINANCE",
        service_name="freqtrade-binance-live",
        template_name="config-binance-live-smoke.json",
        env_name="binance.env",
        config_name="config-binance.json",
        pair_pattern=".+/USDT:USDT",
    ),
    "HYPERLIQUID": WorkerDefinition(
        venue="HYPERLIQUID",
        service_name="freqtrade-hyperliquid-live",
        template_name="config-hyperliquid-live-smoke.json",
        env_name="hyperliquid.env",
        config_name="config-hyperliquid.json",
        pair_pattern=".+/USDC:USDC",
    ),
}


def _asset_dir() -> Path:
    configured = os.environ.get("TRADEOPS_FREQTRADE_ASSET_DIR")
    if configured:
        return Path(configured)
    repository_assets = Path(__file__).resolve().parents[2] / "freqtrade"
    return repository_assets if repository_assets.is_dir() else Path("/app/freqtrade")


def _prepare_output_dir(path: Path) -> Path:
    target = path.resolve()
    if path.is_symlink() or (target.exists() and not target.is_dir()):
        rejections.reject(
            "FREQTRADE_PROVISION_PATH_INVALID",
            "Freqtrade runtime output must be a real directory",
        )
    target.mkdir(parents=True, exist_ok=True)
    os.chown(target, FREQTRADE_UID, FREQTRADE_GID)
    target.chmod(0o700)
    return target


def _write_file(path: Path, content: str, *, mode: int) -> None:
    if path.is_symlink():
        path.unlink()
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chown(temporary, FREQTRADE_UID, FREQTRADE_GID)
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _env_value(value: str) -> str:
    if not value or "\x00" in value or "\n" in value or "\r" in value:
        rejections.reject(
            "FREQTRADE_PROVISION_SECRET_INVALID",
            "Freqtrade runtime secret material is malformed",
        )
    return json.dumps(value, ensure_ascii=True)


def _discover_hyperliquid_hip3_dexes() -> tuple[str, ...]:
    """Read the complete current official HIP-3 DEX directory or fail closed."""

    try:
        response = requests.post(
            HYPERLIQUID_INFO_URL,
            json={"type": "perpDexs"},
            timeout=15,
        )
        if response.status_code != 200:
            raise ValueError("unexpected response status")
        payload = response.json()
    except (requests.RequestException, requests.JSONDecodeError, ValueError) as exc:
        raise domain.DomainRejected(
            "FREQTRADE_HIP3_DIRECTORY_UNAVAILABLE",
            "the official Hyperliquid HIP-3 DEX directory is unavailable",
        ) from exc
    if not isinstance(payload, list):
        rejections.reject(
            "FREQTRADE_HIP3_DIRECTORY_INVALID",
            "the official Hyperliquid HIP-3 DEX directory response is invalid",
        )
    names: list[str] = []
    core_rows = 0
    for item in payload:
        if item is None:
            core_rows += 1
            continue
        if not isinstance(item, dict) or "name" not in item:
            rejections.reject(
                "FREQTRADE_HIP3_DIRECTORY_INVALID",
                "the official Hyperliquid HIP-3 DEX directory contains ambiguous entries",
            )
        name = item["name"]
        if name is None:
            core_rows += 1
        elif isinstance(name, str):
            names.append(name)
        else:
            rejections.reject(
                "FREQTRADE_HIP3_DIRECTORY_INVALID",
                "the official Hyperliquid HIP-3 DEX directory contains invalid identities",
            )
    if core_rows != 1 or not names or len(names) > HYPERLIQUID_HIP3_DEX_LIMIT:
        rejections.reject(
            "FREQTRADE_HIP3_DIRECTORY_INVALID",
            "the official Hyperliquid HIP-3 DEX directory is empty or exceeds its bound",
        )
    try:
        normalized = parse_hip3_dexes(",".join(names))
    except ValueError as exc:
        raise domain.DomainRejected(
            "FREQTRADE_HIP3_DIRECTORY_INVALID",
            "the official Hyperliquid HIP-3 DEX directory contains invalid identities",
        ) from exc
    if len(normalized) != len(names):
        rejections.reject(
            "FREQTRADE_HIP3_DIRECTORY_INVALID",
            "the official Hyperliquid HIP-3 DEX directory contains ambiguous entries",
        )
    return normalized


def _runtime_config(
    template: dict[str, Any],
    definition: WorkerDefinition,
    account_id: str,
    *,
    hip3_dexes: tuple[str, ...] = (),
) -> str:
    config = json.loads(json.dumps(template))
    config.update(
        {
            "bot_name": f"tradeops-{definition.venue.lower()}-live-{account_id}",
            "dry_run": False,
            "initial_state": "running",
            "force_entry_enable": True,
            "max_open_trades": -1,
            "position_adjustment_enable": True,
            "cancel_open_orders_on_exit": False,
            # The strategy never emits autonomous signals.  A slower candle
            # cadence keeps the complete executable catalog available to
            # Force Entry without continuously consuming venue read limits.
            "timeframe": CONTROL_PLANE_TIMEFRAME,
        }
    )
    exchange = config.get("exchange")
    api_server = config.get("api_server")
    telegram = config.get("telegram")
    if not isinstance(exchange, dict) or not isinstance(api_server, dict):
        rejections.reject(
            "FREQTRADE_PROVISION_TEMPLATE_INVALID",
            "Freqtrade production template is invalid",
        )
    exchange["pair_whitelist"] = [definition.pair_pattern]
    exchange["pair_blacklist"] = []
    if definition.venue == "HYPERLIQUID":
        if not hip3_dexes:
            rejections.reject(
                "FREQTRADE_HIP3_DIRECTORY_INVALID",
                "the Hyperliquid LIVE Worker requires the complete official HIP-3 DEX directory",
            )
        exchange["hip3_dexes"] = list(hip3_dexes)
        for key in ("ccxt_config", "ccxt_async_config"):
            ccxt = exchange.get(key)
            if not isinstance(ccxt, dict):
                rejections.reject(
                    "FREQTRADE_PROVISION_TEMPLATE_INVALID",
                    "Freqtrade production template lacks CCXT rate limiting",
                )
            ccxt["enableRateLimit"] = True
            ccxt["rateLimit"] = HYPERLIQUID_READ_RATE_LIMIT_MS
            options = ccxt.setdefault("options", {})
            if not isinstance(options, dict):
                rejections.reject(
                    "FREQTRADE_PROVISION_TEMPLATE_INVALID",
                    "Freqtrade production template has invalid CCXT options",
                )
            options["defaultType"] = "swap"
            options["fetchMarkets"] = {
                "types": ["swap", "hip3"],
                "hip3": {
                    "dexes": list(hip3_dexes),
                    "limit": len(hip3_dexes),
                },
            }
    api_server.update(
        {
            "enabled": True,
            "listen_ip_address": "0.0.0.0",  # noqa: S104 - internal Compose network only
            "listen_port": 8080,
            "enable_openapi": False,
            "username": "overridden-by-environment",
            "password": "overridden-by-environment",
            "jwt_secret_key": "overridden-by-environment",
            "ws_token": "overridden-by-environment",
            "CORS_origins": [],
        }
    )
    if isinstance(telegram, dict):
        telegram["enabled"] = False
        telegram["token"] = ""
        telegram["chat_id"] = ""
        telegram["authorized_users"] = []
    return json.dumps(config, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def _system_admin_id(database: Database, team_id: UUID) -> UUID:
    with database.session_factory() as session:
        admins = session.scalars(
            select(models.User)
            .join(
                models.RoleAssignment,
                models.RoleAssignment.user_id == models.User.user_id,
            )
            .where(
                models.User.active.is_(True),
                models.User.principal_type == domain.PrincipalType.HUMAN.value,
                models.User.active_team_id == team_id,
                models.RoleAssignment.team_id == team_id,
                models.RoleAssignment.role == domain.Role.SYSTEM_ADMIN.value,
            )
            .order_by(models.User.user_id)
        ).all()
    if len(admins) != 1:
        rejections.reject(
            "FREQTRADE_PROVISION_ADMIN_SCOPE_INVALID",
            "automatic Worker provisioning requires one exact active system administrator",
        )
    return admins[0].user_id


def _live_accounts(database: Database) -> tuple[models.ExchangeAccount, ...]:
    with database.session_factory() as session:
        rows = session.scalars(
            select(models.ExchangeAccount)
            .where(
                models.ExchangeAccount.active.is_(True),
                models.ExchangeAccount.deleted_at.is_(None),
                models.ExchangeAccount.environment == domain.ExecutionEnvironment.LIVE.value,
                models.ExchangeAccount.venue.in_(SUPPORTED_VENUES),
                models.ExchangeAccount.connection_status == "VERIFIED",
                models.ExchangeAccount.runtime_sync_enabled.is_(True),
                models.ExchangeAccount.credentials_ciphertext.is_not(None),
                models.ExchangeAccount.credential_version >= 1,
            )
            .order_by(models.ExchangeAccount.venue, models.ExchangeAccount.account_id)
        ).all()
    by_venue = {venue: [row for row in rows if row.venue == venue] for venue in SUPPORTED_VENUES}
    if any(len(by_venue[venue]) != 1 for venue in SUPPORTED_VENUES):
        rejections.reject(
            "FREQTRADE_PROVISION_ACCOUNT_SCOPE_INVALID",
            "automatic Worker provisioning requires one exact verified LIVE account per venue",
        )
    return tuple(by_venue[venue][0] for venue in SUPPORTED_VENUES)


def _binding_map(
    service: TradingService,
) -> dict[UUID, PreparedFreqtradeWorkerBinding]:
    return {
        binding.exchange_account_id: binding
        for binding in service.runtime_freqtrade_worker_bindings(verified_only=False)
    }


def _ensure_binding(
    service: TradingService,
    account: models.ExchangeAccount,
    definition: WorkerDefinition,
    *,
    hip3_dexes: tuple[str, ...],
    now: datetime,
) -> tuple[PreparedFreqtradeWorkerBinding, bool]:
    current = _binding_map(service).get(account.exchange_account_id)
    desired_name = definition.worker_name(account.exchange_account_id)
    desired_hip3_dexes = (
        hip3_dexes if definition.venue == "HYPERLIQUID" else ()
    )
    if current is not None and (
        current.worker_name == desired_name
        and current.worker_url == definition.worker_url
        and current.worker_mode == "LIVE"
        and current.hip3_dexes == desired_hip3_dexes
        and current.ws_token is not None
    ):
        return current, True
    actor_id = _system_admin_id(service.database, account.team_id)
    service.configure_exchange_account_freqtrade_worker(
        account.exchange_account_id,
        actor_id=actor_id,
        mode="LIVE",
        name=desired_name,
        base_url=definition.worker_url,
        username=f"tradeops-{definition.venue.lower()}",
        password=secrets.token_urlsafe(48),
        ws_token=secrets.token_urlsafe(48),
        hip3_dexes=desired_hip3_dexes,
        expected_version=account.version,
        idempotency_key=(
            f"auto-freqtrade-config-{account.exchange_account_id}-{account.version}-"
            f"{hashlib.sha256(','.join(desired_hip3_dexes).encode()).hexdigest()[:16]}"
        ),
        now=now,
    )
    prepared = _binding_map(service).get(account.exchange_account_id)
    if prepared is None:
        rejections.reject(
            "FREQTRADE_PROVISION_BINDING_INVALID",
            "automatic Worker binding was not persisted",
        )
    return prepared, False


def _exchange_credentials(
    service: TradingService,
    exchange_account_id: UUID,
) -> tuple[models.ExchangeAccount, dict[str, str]]:
    with service.database.session_factory() as session:
        account = session.get(models.ExchangeAccount, exchange_account_id)
        if account is None or account.credentials_ciphertext is None:
            rejections.reject(
                "FREQTRADE_PROVISION_ACCOUNT_SCOPE_INVALID",
                "the exact exchange credential binding is unavailable",
            )
        credentials = service.credential_cipher.decrypt(
            account.credentials_ciphertext,
            team_id=account.team_id,
            exchange_account_id=account.exchange_account_id,
            venue=account.venue,
            credential_version=account.credential_version,
        )
        session.expunge(account)
    required = (
        {"api_key", "api_secret"}
        if account.venue == "BINANCE"
        else {"account_address", "api_wallet_address", "api_wallet_private_key"}
    )
    if not required.issubset(credentials):
        rejections.reject(
            "FREQTRADE_EXCHANGE_SIGNING_MATERIAL_MISSING",
            "the exact LIVE account lacks complete encrypted trading signing material",
        )
    return account, credentials


def _worker_environment(
    definition: WorkerDefinition,
    binding: PreparedFreqtradeWorkerBinding,
    credentials: dict[str, str],
) -> str:
    if binding.ws_token is None:
        rejections.reject(
            "FREQTRADE_RPC_AUTH_REQUIRED",
            "the exact Worker binding lacks an RPC WebSocket token",
        )
    jwt_secret = hmac.new(
        binding.ws_token.encode(),
        binding.password.encode(),
        hashlib.sha256,
    ).hexdigest()
    values = {
        "FREQTRADE__API_SERVER__USERNAME": binding.username,
        "FREQTRADE__API_SERVER__PASSWORD": binding.password,
        "FREQTRADE__API_SERVER__JWT_SECRET_KEY": jwt_secret,
        "FREQTRADE__API_SERVER__WS_TOKEN": binding.ws_token,
        "FREQTRADE__DRY_RUN": "false",
        "FREQTRADE__FORCE_ENTRY_ENABLE": "true",
        "FREQTRADE__INITIAL_STATE": "running",
        "FREQTRADE__TELEGRAM__ENABLED": "false",
        "FREQTRADE__TELEGRAM__AUTHORIZED_USERS": "[]",
    }
    if definition.venue == "BINANCE":
        values.update(
            {
                "FREQTRADE__EXCHANGE__KEY": credentials["api_key"],
                "FREQTRADE__EXCHANGE__SECRET": credentials["api_secret"],
            }
        )
    else:
        values.update(
            {
                "FREQTRADE__EXCHANGE__WALLET_ADDRESS": credentials["account_address"],
                "FREQTRADE__EXCHANGE__PRIVATE_KEY": credentials[
                    "api_wallet_private_key"
                ],
            }
        )
    return "".join(f"{key}={_env_value(value)}\n" for key, value in sorted(values.items()))


def prepare(output_dir: Path) -> list[dict[str, object]]:
    settings = get_settings()
    settings.validate_runtime_security()
    database = Database(settings.database_url)
    service = TradingService(
        database,
        credential_encryption_key=settings.credential_encryption_key,
    )
    target = _prepare_output_dir(output_dir)
    assets = _asset_dir()
    now = datetime.now(UTC)
    results: list[dict[str, object]] = []
    try:
        accounts = _live_accounts(database)
        hyperliquid_hip3_dexes = _discover_hyperliquid_hip3_dexes()
        for stale in target.glob("*.env"):
            if stale.name not in {definition.env_name for definition in WORKERS.values()}:
                stale.unlink()
        for account in accounts:
            definition = WORKERS[account.venue]
            binding, reused = _ensure_binding(
                service,
                account,
                definition,
                hip3_dexes=hyperliquid_hip3_dexes,
                now=now,
            )
            current_account, credential_values = _exchange_credentials(
                service,
                account.exchange_account_id,
            )
            template_path = assets / definition.template_name
            strategy_path = assets / "strategies" / "ControlPlaneOnlyStrategy.py"
            if not template_path.is_file() or not strategy_path.is_file():
                rejections.reject(
                    "FREQTRADE_PROVISION_ASSET_MISSING",
                    "versioned Freqtrade production assets are unavailable",
                )
            template = json.loads(template_path.read_text(encoding="utf-8"))
            _write_file(
                target / definition.config_name,
                _runtime_config(
                    template,
                    definition,
                    current_account.account_id,
                    hip3_dexes=binding.hip3_dexes,
                ),
                mode=0o644,
            )
            _write_file(
                target / definition.env_name,
                _worker_environment(definition, binding, credential_values),
                mode=0o600,
            )
            results.append(
                {
                    "account_id": current_account.account_id,
                    "auth_reused": reused,
                    "exchange_account_id": str(current_account.exchange_account_id),
                    "mode": "LIVE",
                    "order_send": "none",
                    "venue": current_account.venue,
                    "worker": binding.worker_name,
                }
            )
        _write_file(
            target / "ControlPlaneOnlyStrategy.py",
            strategy_path.read_text(encoding="utf-8"),
            mode=0o644,
        )
        patch_dir = assets / "patches"
        for name in ("sitecustomize.py", "portfolio_margin_compat.py"):
            source = patch_dir / name
            if source.is_file():
                _write_file(
                    target / name,
                    source.read_text(encoding="utf-8"),
                    mode=0o644,
                )
        return results
    finally:
        database.dispose()


def _expected_pairs(
    database: Database,
    binding: PreparedFreqtradeWorkerBinding,
) -> set[str]:
    with database.session_factory() as session:
        instruments = session.scalars(
            select(models.Instrument)
            .where(
                models.Instrument.venue == binding.venue,
                models.Instrument.active.is_(True),
            )
            .order_by(models.Instrument.symbol)
        ).all()
    expected = {
        freqtrade_pair(binding.venue, item.symbol, hip3_dexes=binding.hip3_dexes)
        for item in instruments
    }
    if not expected:
        rejections.reject(
            "FREQTRADE_CATALOG_EMPTY",
            "the current official executable Instrument Catalog is empty",
        )
    return expected


async def _verify_rpc(client: FreqtradeWorkerClient) -> None:
    await client.verify_rpc_connection()


def verify(*, record: bool) -> list[dict[str, object]]:
    settings = get_settings()
    settings.validate_runtime_security()
    database = Database(settings.database_url)
    service = TradingService(
        database,
        credential_encryption_key=settings.credential_encryption_key,
    )
    results: list[dict[str, object]] = []
    try:
        bindings = {
            binding.venue: binding
            for binding in service.runtime_freqtrade_worker_bindings(verified_only=False)
            if binding.environment == domain.ExecutionEnvironment.LIVE.value
            and binding.venue in SUPPORTED_VENUES
        }
        if set(bindings) != set(SUPPORTED_VENUES):
            rejections.reject(
                "FREQTRADE_PROVISION_BINDING_INVALID",
                "both exact LIVE Worker bindings must be configured",
            )
        for venue in SUPPORTED_VENUES:
            binding = bindings[venue]
            client = FreqtradeWorkerClient(
                FreqtradeWorkerSpec(
                    name=binding.worker_name,
                    venue=binding.venue,  # type: ignore[arg-type]
                    base_url=binding.worker_url,
                    username=binding.username,
                    password=binding.password,
                    ws_token=binding.ws_token,
                    hip3_dexes=binding.hip3_dexes,
                    exchange_account_id=str(binding.exchange_account_id),
                    team_id=str(binding.team_id),
                    account_id=binding.account_id,
                ),
                timeout_seconds=settings.freqtrade_timeout_seconds,
                confirmation_timeout_seconds=settings.freqtrade_confirmation_timeout_seconds,
            )
            probe = client.probe(expected_mode="LIVE")
            expected = _expected_pairs(database, binding)
            observed = set(probe.get("whitelist", []))
            if observed != expected:
                rejections.reject(
                    "FREQTRADE_CATALOG_MISMATCH",
                    (
                        "the LIVE Worker whitelist does not match the complete "
                        "official executable catalog"
                    ),
                )
            asyncio.run(_verify_rpc(client))
            open_trades = client.open_trades()
            if record:
                actor_id = _system_admin_id(database, binding.team_id)
                prepared, replay = service.prepare_exchange_account_freqtrade_verification(
                    binding.exchange_account_id,
                    actor_id=actor_id,
                    expected_version=binding.account_version,
                    idempotency_key=(
                        f"auto-freqtrade-verify-{binding.exchange_account_id}-"
                        f"{binding.account_version}"
                    ),
                )
                if replay is None:
                    assert prepared is not None
                    service.record_exchange_account_freqtrade_verification(
                        prepared,
                        actor_id=actor_id,
                        error_code=None,
                        idempotency_key=(
                            f"auto-freqtrade-verify-{binding.exchange_account_id}-"
                            f"{binding.account_version}"
                        ),
                        now=datetime.now(UTC),
                        probe_result=probe,
                    )
            results.append(
                {
                    "account_id": binding.account_id,
                    "catalog_count": len(expected),
                    "force_entry_available": probe.get("force_entry_enabled") is True,
                    "mode": "LIVE",
                    "open_trade_count": len(open_trades),
                    "order_send": "none",
                    "rpc_websocket": "READY",
                    "status": "READY",
                    "venue": venue,
                    "worker": binding.worker_name,
                }
            )
        return results
    finally:
        database.dispose()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Provision exact-account Freqtrade LIVE Workers without external orders"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--output-dir", type=Path, required=True)
    subparsers.add_parser("verify")
    subparsers.add_parser("check")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "prepare":
        result = prepare(args.output_dir)
    else:
        result = verify(record=args.command == "verify")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
