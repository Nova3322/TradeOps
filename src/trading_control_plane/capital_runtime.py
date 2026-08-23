from __future__ import annotations

import argparse
import json
import logging
import signal
import threading
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from trading_control_plane.adapters.capital import (
    CapitalAdapter,
    CapitalAdapterFactory,
    CapitalOperation,
    CapitalScope,
    build_production_capital_adapter_factory,
)
from trading_control_plane.binance_state import DatabaseBinanceRequestState
from trading_control_plane.config import Settings, get_settings
from trading_control_plane.database import Database
from trading_control_plane.domain import DomainRejected
from trading_control_plane.logging import configure_logging
from trading_control_plane.service import TradingService

logger = logging.getLogger(__name__)


class BinanceDepositContinuationWorker:
    """Durably continue only system-recorded Binance deposits after the browser exits."""

    def __init__(
        self,
        *,
        settings: Settings,
        database: Database,
        adapter_factory: CapitalAdapterFactory | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.settings = settings
        self.database = database
        self.clock = clock
        self.service = TradingService(
            database,
            credential_encryption_key=settings.credential_encryption_key,
        )
        if adapter_factory is None:
            request_state = DatabaseBinanceRequestState(database)

            def credentials(scope: CapitalScope) -> Mapping[str, str]:
                binding = self.service.verified_capital_account_binding(
                    workspace_id=UUID(scope.workspace_id),
                    team_id=UUID(scope.team_id),
                    account_id=scope.account_id,
                    venue=scope.venue,
                    environment=scope.environment,
                )
                return binding.credentials

            adapter_factory = build_production_capital_adapter_factory(
                binance_account_id=settings.binance_capital_account_id,
                binance_api_key=settings.binance_capital_api_key,
                binance_api_secret=settings.binance_capital_api_secret,
                binance_base_url=settings.binance_capital_base_url,
                binance_recv_window_ms=settings.binance_recv_window_ms,
                binance_timeout_seconds=settings.binance_capital_timeout_seconds,
                binance_request_state=request_state,
                credential_resolver=credentials,
            )
        self.adapter_factory = adapter_factory

    def _adapter(self, claim: Mapping[str, Any]) -> CapitalAdapter:
        return self.adapter_factory(
            CapitalScope(
                workspace_id=str(claim["workspace_id"]),
                team_id=str(claim["team_id"]),
                account_id=str(claim["account_id"]),
                venue="BINANCE",
                environment="LIVE",
                account_mode=str(claim["account_mode"]),
            )
        )

    def _finish_error(
        self,
        claim: Mapping[str, Any],
        *,
        error_code: str,
        outbox_unknown: bool = False,
    ) -> None:
        now = self.clock()
        operation_id = UUID(str(claim["operation_id"]))
        if outbox_unknown:
            self.service.mark_direct_capital_binance_deposit_transfer_unknown(
                operation_id,
                error_code=error_code,
                now=now,
            )
        self.service.finish_direct_capital_binance_deposit_check(
            operation_id,
            poll_token=str(claim["poll_token"]),
            error_code=error_code,
            now=now,
        )

    def _run_claim(self, claim: Mapping[str, Any]) -> str:
        operation_id = UUID(str(claim["operation_id"]))
        adapter = self._adapter(claim)
        try:
            deposit = adapter.execute(
                CapitalOperation.BINANCE_VERIFY_DEPOSIT,
                {
                    "transaction_hash": str(claim["transaction_hash"]),
                    "destination": str(claim["destination"]),
                    "amount": Decimal(str(claim["minimum_amount"])),
                },
            ).value
        except DomainRejected as exc:
            self._finish_error(claim, error_code=exc.code)
            return exc.code
        if not isinstance(deposit, dict):
            self._finish_error(claim, error_code="BINANCE_CAPITAL_RESPONSE_INVALID")
            return "BINANCE_CAPITAL_RESPONSE_INVALID"
        try:
            transfer_claim = self.service.claim_direct_capital_binance_deposit_internal_transfer(
                operation_id,
                poll_token=str(claim["poll_token"]),
                deposit_evidence=deposit,
                now=self.clock(),
            )
        except DomainRejected as exc:
            self._finish_error(claim, error_code=exc.code)
            return exc.code
        mode = str(transfer_claim["mode"])
        if mode == "CONFIRMED":
            internal_transfer: dict[str, Any] = {
                "type": "MAIN_UMFUTURE",
                "asset": "USDC",
                "amount": str(transfer_claim["amount"]),
                "status": "CONFIRMED",
                "tranId": transfer_claim["external_reference"],
            }
        else:
            operation = (
                CapitalOperation.BINANCE_COMPLETE_DEPOSIT
                if mode == "SUBMIT"
                else CapitalOperation.BINANCE_VERIFY_DEPOSIT_INTERNAL_TRANSFER
            )
            try:
                result = adapter.execute(
                    operation,
                    {
                        "amount": Decimal(str(transfer_claim["amount"])),
                        "prepared_at": transfer_claim["prepared_at"],
                        "now": self.clock(),
                        **(
                            {"operation_id": str(operation_id)}
                            if mode == "SUBMIT"
                            else {}
                        ),
                    },
                ).value
            except DomainRejected as exc:
                self._finish_error(claim, error_code=exc.code, outbox_unknown=True)
                return exc.code
            if not isinstance(result, dict):
                self._finish_error(
                    claim,
                    error_code="CAPITAL_RESULT_UNKNOWN",
                    outbox_unknown=True,
                )
                return "CAPITAL_RESULT_UNKNOWN"
            internal_transfer = result
        try:
            self.service.confirm_direct_capital_binance_deposit_continuation(
                operation_id,
                poll_token=str(claim["poll_token"]),
                deposit_evidence=deposit,
                internal_transfer=internal_transfer,
                now=self.clock(),
            )
        except DomainRejected as exc:
            self._finish_error(claim, error_code=exc.code, outbox_unknown=True)
            return exc.code
        return "CONFIRMED"

    def run_once(self) -> dict[str, object]:
        selected = 0
        confirmed = 0
        outcomes: dict[str, int] = {}
        for _ in range(self.settings.capital_continuation_worker_batch_size):
            claim = self.service.claim_due_direct_capital_binance_deposit(now=self.clock())
            if claim is None:
                break
            selected += 1
            try:
                outcome = self._run_claim(claim)
            except Exception:
                logger.exception(
                    "Binance deposit continuation failed unexpectedly",
                    extra={
                        "event": "capital_binance_deposit_continuation_unexpected",
                        "component": "capital-continuation-worker",
                        "operation_id": claim["operation_id"],
                    },
                )
                self._finish_error(claim, error_code="CAPITAL_RESULT_UNKNOWN")
                outcome = "CAPITAL_RESULT_UNKNOWN"
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
            if outcome == "CONFIRMED":
                confirmed += 1
        return {"selected": selected, "confirmed": confirmed, "outcomes": outcomes}

    def run_forever(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            report = self.run_once()
            logger.info(
                "Capital continuation cycle completed",
                extra={
                    "event": "capital_continuation_cycle_completed",
                    "component": "capital-continuation-worker",
                    **report,
                },
            )
            if stop_event.wait(self.settings.capital_continuation_worker_scan_seconds):
                break


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run durable capital continuations")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="run one continuation cycle")
    mode.add_argument("--healthcheck", action="store_true", help="validate worker readiness")
    args = parser.parse_args(argv)
    settings = get_settings()
    settings.validate_runtime_security()
    configure_logging(settings.log_level)
    if not settings.capital_continuation_worker_enabled:
        raise SystemExit("TRADING_CAPITAL_CONTINUATION_WORKER_ENABLED must be true")
    if not settings.credential_encryption_key:
        raise SystemExit("TRADING_CREDENTIAL_ENCRYPTION_KEY is required")
    database = Database(settings.database_url)
    try:
        ready, reason = database.is_ready()
        if not ready:
            raise SystemExit(f"database is not ready: {reason}")
        if args.healthcheck:
            print(
                json.dumps(
                    {
                        "component": "capital-continuation-worker",
                        "database": "READY",
                        "status": "READY",
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
            return 0
        worker = BinanceDepositContinuationWorker(settings=settings, database=database)
        if args.once:
            print(json.dumps(worker.run_once(), separators=(",", ":"), sort_keys=True))
            return 0
        stop_event = threading.Event()
        for signal_number in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signal_number, lambda _signal, _frame: stop_event.set())
        worker.run_forever(stop_event)
        return 0
    finally:
        database.dispose()


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["BinanceDepositContinuationWorker", "main"]
