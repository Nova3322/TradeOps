from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from conftest import add_exchange_account_fixture
from sqlalchemy import select

from trading_control_plane.adapters.capital import CapitalOperation, CapitalResult, CapitalScope
from trading_control_plane.capital_runtime import BinanceDepositContinuationWorker
from trading_control_plane.config import Settings
from trading_control_plane.database import Database
from trading_control_plane.domain import CapabilityStatus, DomainRejected
from trading_control_plane.models import (
    BinanceCapitalOutbox,
    DirectCapitalOperation,
    Team,
    User,
)
from trading_control_plane.service import TradingService

NOW = datetime(2026, 8, 21, 10, tzinfo=UTC)
TX_HASH = "0x" + "ab" * 32
SAFE = "0x1111111111111111111111111111111111111111"
BINANCE_DEPOSIT = "0x2222222222222222222222222222222222222222"
TEST_CREDENTIAL_KEY = base64.urlsafe_b64encode(b"capital-continuation-test-key-32"[:32]).decode()


class ContinuationAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[CapitalOperation, dict[str, Any]]] = []
        self.transfer_reconciled = False
        self.deposit_checks = 0

    def execute(
        self,
        operation: CapitalOperation,
        parameters: dict[str, Any],
    ) -> CapitalResult:
        self.calls.append((operation, dict(parameters)))
        if operation is CapitalOperation.BINANCE_VERIFY_DEPOSIT:
            self.deposit_checks += 1
            value = {
                "depositId": "deposit-1",
                "status": "CONFIRMED",
                "transactionHash": TX_HASH,
                "amount": "10.2500" if self.deposit_checks == 1 else "10.25",
                "destination": BINANCE_DEPOSIT,
                "network": "ARBITRUM",
                "asset": "USDC",
            }
        elif operation is CapitalOperation.BINANCE_COMPLETE_DEPOSIT:
            raise DomainRejected(
                "BINANCE_INTERNAL_TRANSFER_PENDING",
                "fixture transfer accepted before history became visible",
            )
        elif operation is CapitalOperation.BINANCE_VERIFY_DEPOSIT_INTERNAL_TRANSFER:
            self.transfer_reconciled = True
            value = {
                "type": "MAIN_UMFUTURE",
                "asset": "USDC",
                "amount": "10.25",
                "status": "CONFIRMED",
                "tranId": 67890,
            }
        else:  # pragma: no cover - protects the fixture contract
            raise AssertionError(operation)
        return CapitalResult(backend="fixture", contract="fixture", value=value)


class MissingDepositAdapter:
    def execute(
        self,
        operation: CapitalOperation,
        parameters: dict[str, Any],
    ) -> CapitalResult:
        assert operation is CapitalOperation.BINANCE_VERIFY_DEPOSIT
        assert parameters["transaction_hash"] == TX_HASH
        raise DomainRejected(
            "BINANCE_CAPITAL_RECEIPT_NOT_FOUND",
            "fixture exact deposit is absent",
        )


def _scheduled_operation(
    database: Database,
    *,
    username: str,
    receipt_attempt_count: int = 0,
) -> tuple[TradingService, DirectCapitalOperation]:
    service = TradingService(database)
    actor_id = service.bootstrap_admin(username, now=NOW)
    add_exchange_account_fixture(database, actor_id, "binance-main", "BINANCE")
    service.set_capability_gate(
        "CAPITAL_TRANSFER",
        CapabilityStatus.ENABLED,
        "capital continuation integration fixture",
        actor_id,
        now=NOW,
    )
    with database.session_factory.begin() as session:
        user = session.get(User, actor_id)
        assert user is not None and user.active_team_id is not None
        team = session.get(Team, user.active_team_id)
        assert team is not None
        item = DirectCapitalOperation(
            team_id=team.team_id,
            environment="LIVE",
            treasury_provider="SAFE_SPENDING_LIMIT",
            path="VAULT_TO_BINANCE",
            status="AWAITING_RECEIPT",
            receipt_status="PENDING",
            account_id="binance-main",
            venue="BINANCE",
            vault_id="safe-main",
            asset="USDC",
            network="ARBITRUM",
            amount=Decimal("10.25"),
            max_fee=Decimal("0"),
            min_received=Decimal("10"),
            source_reference=SAFE,
            destination_reference=BINANCE_DEPOSIT,
            stages=[
                {
                    "code": "BINANCE_DEPOSIT_PREFLIGHT_READY",
                    "status": "READY",
                    "artifact": {
                        "kind": "BINANCE_ARBITRUM_DEPOSIT_PREFLIGHT",
                        "asset": "USDC",
                        "network": "ARBITRUM",
                        "destination": BINANCE_DEPOSIT,
                        "amount": "10",
                    },
                },
                {
                    "code": "TREASURY_WITHDRAWAL_SUBMITTED_BY_HUMAN_WALLET",
                    "status": "AWAITING_RECEIPT",
                    "transaction_hash": TX_HASH,
                },
            ],
            blockers=[],
            receipt_next_due_at=NOW,
            receipt_attempt_count=receipt_attempt_count,
            execute_after=None,
            expires_at=NOW + timedelta(minutes=5),
            final_confirmed_at=NOW,
            actor_id=actor_id,
            correlation_id=uuid4(),
            idempotency_key=f"{username}-operation",
            version=1,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(item)
        session.flush()
        operation_id = item.operation_id
    with database.session_factory() as session:
        stored = session.get(DirectCapitalOperation, operation_id)
        assert stored is not None
        session.expunge(stored)
        return service, stored


def _outbound_operation(database: Database) -> tuple[TradingService, DirectCapitalOperation, UUID]:
    service = TradingService(database)
    actor_id = service.bootstrap_admin("capital-withdraw-fence-admin", now=NOW)
    add_exchange_account_fixture(database, actor_id, "binance-main", "BINANCE")
    service.set_capability_gate(
        "CAPITAL_TRANSFER",
        CapabilityStatus.ENABLED,
        "capital withdrawal fence integration fixture",
        actor_id,
        now=NOW,
    )
    artifact = {
        "kind": "BINANCE_RESTRICTED_WITHDRAWAL_PREFLIGHT",
        "asset": "USDC",
        "network": "ARBITRUM",
        "destination": SAFE,
        "amount": "6",
        "withdrawOrderId": "operation-fixture",
    }
    with database.session_factory.begin() as session:
        user = session.get(User, actor_id)
        assert user is not None and user.active_team_id is not None
        team = session.get(Team, user.active_team_id)
        assert team is not None
        item = DirectCapitalOperation(
            team_id=team.team_id,
            environment="LIVE",
            treasury_provider="SAFE_SPENDING_LIMIT",
            path="BINANCE_TO_VAULT",
            status="UNSIGNED_PLAN_READY",
            receipt_status="NOT_SUBMITTED",
            account_id="binance-main",
            venue="BINANCE",
            vault_id="safe-main",
            asset="USDC",
            network="ARBITRUM",
            amount=Decimal("6"),
            max_fee=Decimal("1"),
            min_received=Decimal("5"),
            source_reference="binance-main",
            destination_reference=SAFE,
            stages=[
                {
                    "code": "BINANCE_RESTRICTED_WITHDRAWAL_PREFLIGHT_READY",
                    "status": "READY",
                    "artifact": artifact,
                }
            ],
            blockers=[],
            execute_after=None,
            expires_at=NOW + timedelta(minutes=5),
            final_confirmed_at=NOW,
            actor_id=actor_id,
            correlation_id=uuid4(),
            idempotency_key="capital-withdraw-fence-operation",
            version=1,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(item)
        session.flush()
        operation_id = item.operation_id
    with database.session_factory() as session:
        stored = session.get(DirectCapitalOperation, operation_id)
        assert stored is not None
        session.expunge(stored)
    return service, stored, actor_id


def _worker(
    database: Database,
    adapter: ContinuationAdapter | MissingDepositAdapter,
    clock: list[datetime],
) -> BinanceDepositContinuationWorker:
    settings = Settings(
        environment="test",
        database_url=str(database.engine.url),
        credential_encryption_key=TEST_CREDENTIAL_KEY,
        capital_continuation_worker_enabled=True,
    )

    def factory(_scope: CapitalScope):
        return adapter

    return BinanceDepositContinuationWorker(
        settings=settings,
        database=database,
        adapter_factory=factory,
        clock=lambda: clock[0],
    )


def test_worker_uses_exact_credited_amount_and_reconciles_without_duplicate_post(
    database: Database,
) -> None:
    service, item = _scheduled_operation(database, username="capital-continuation-admin")
    with pytest.raises(DomainRejected) as browser_claim:
        service.acquire_direct_capital_binance_receipt_poll(
            item.operation_id,
            item.actor_id,
            expected_version=1,
            stage="BINANCE_DEPOSIT",
            token="stale-browser-fixture",  # noqa: S106 - inert lease token
            now=NOW,
        )
    assert browser_claim.value.code == "BINANCE_DEPOSIT_CONTINUATION_WORKER_OWNED"
    adapter = ContinuationAdapter()
    clock = [NOW]
    worker = _worker(database, adapter, clock)

    first = worker.run_once()

    assert first == {
        "selected": 1,
        "confirmed": 0,
        "outcomes": {"BINANCE_INTERNAL_TRANSFER_PENDING": 1},
    }
    assert [call[0] for call in adapter.calls] == [
        CapitalOperation.BINANCE_VERIFY_DEPOSIT,
        CapitalOperation.BINANCE_COMPLETE_DEPOSIT,
    ]
    assert adapter.calls[1][1]["amount"] == Decimal("10.25")
    with database.session_factory() as session:
        outbox = session.scalar(
            select(BinanceCapitalOutbox).where(
                BinanceCapitalOutbox.operation_id == item.operation_id,
                BinanceCapitalOutbox.stage == "DEPOSIT_SPOT_TO_USDM",
            )
        )
        pending = session.get(DirectCapitalOperation, item.operation_id)
        assert outbox is not None and outbox.status == "UNKNOWN"
        assert outbox.attempt_count == 1
        assert pending is not None and pending.receipt_attempt_count == 1
        assert pending.receipt_next_due_at == NOW + timedelta(minutes=5)

    clock[0] = NOW + timedelta(minutes=5)
    second = worker.run_once()
    third = worker.run_once()

    assert second == {"selected": 1, "confirmed": 1, "outcomes": {"CONFIRMED": 1}}
    assert third == {"selected": 0, "confirmed": 0, "outcomes": {}}
    assert [call[0] for call in adapter.calls].count(
        CapitalOperation.BINANCE_COMPLETE_DEPOSIT
    ) == 1
    assert [call[0] for call in adapter.calls].count(
        CapitalOperation.BINANCE_VERIFY_DEPOSIT_INTERNAL_TRANSFER
    ) == 1
    with database.session_factory() as session:
        settled = session.get(DirectCapitalOperation, item.operation_id)
        outbox = session.scalar(
            select(BinanceCapitalOutbox).where(
                BinanceCapitalOutbox.operation_id == item.operation_id,
                BinanceCapitalOutbox.stage == "DEPOSIT_SPOT_TO_USDM",
            )
        )
        assert settled is not None and settled.status == "SETTLED"
        assert settled.receipt_status == "CONFIRMED"
        assert settled.receipt_next_due_at is None
        assert outbox is not None and outbox.status == "CONFIRMED"
        assert outbox.attempt_count == 1


def test_worker_stops_after_tenth_exact_deposit_check(database: Database) -> None:
    _service, item = _scheduled_operation(
        database,
        username="capital-continuation-exhaust-admin",
        receipt_attempt_count=9,
    )
    adapter = MissingDepositAdapter()
    clock = [NOW]
    worker = _worker(database, adapter, clock)

    tenth = worker.run_once()
    after_limit = worker.run_once()

    assert tenth == {
        "selected": 1,
        "confirmed": 0,
        "outcomes": {"BINANCE_CAPITAL_RECEIPT_NOT_FOUND": 1},
    }
    assert after_limit == {"selected": 0, "confirmed": 0, "outcomes": {}}
    with database.session_factory() as session:
        stopped = session.get(DirectCapitalOperation, item.operation_id)
        assert stopped is not None and stopped.receipt_attempt_count == 10
        assert stopped.status == "UNKNOWN"
        assert stopped.receipt_status == "UNKNOWN"
        assert stopped.receipt_next_due_at is None
        assert "BINANCE_DEPOSIT_CONTINUATION_EXHAUSTED" in stopped.blockers
        assert "BINANCE_CAPITAL_RECEIPT_NOT_FOUND" in stopped.blockers


def test_withdrawal_unknown_result_persists_blocker_and_prevents_blind_retry(
    database: Database,
) -> None:
    service, item, actor_id = _outbound_operation(database)
    artifact = item.stages[0]["artifact"]
    assert isinstance(artifact, dict)

    claimed_version = service.claim_direct_capital_binance_withdrawal_submission(
        item.operation_id,
        actor_id,
        expected_version=1,
        artifact=artifact,
        idempotency_key="withdraw-submit-fixture",
        now=NOW,
    )
    failed_version = service.record_direct_capital_binance_submission_failure(
        item.operation_id,
        actor_id,
        claimed_version=claimed_version,
        error_code="BINANCE_INTERNAL_TRANSFER_PENDING",
        idempotency_key="withdraw-submit-fixture",
        now=NOW + timedelta(seconds=1),
    )

    assert (claimed_version, failed_version) == (2, 3)
    with database.session_factory() as session:
        failed = session.get(DirectCapitalOperation, item.operation_id)
        outbox = session.scalar(
            select(BinanceCapitalOutbox).where(
                BinanceCapitalOutbox.operation_id == item.operation_id,
                BinanceCapitalOutbox.stage == "WITHDRAWAL",
            )
        )
        assert failed is not None and failed.status == "UNKNOWN"
        assert failed.receipt_status == "UNKNOWN"
        assert failed.blockers == ["BINANCE_INTERNAL_TRANSFER_PENDING"]
        assert outbox is not None and outbox.status == "UNKNOWN"
        assert outbox.attempt_count == 1

    with pytest.raises(DomainRejected) as caught:
        service.claim_direct_capital_binance_withdrawal_submission(
            item.operation_id,
            actor_id,
            expected_version=failed_version,
            artifact=artifact,
            idempotency_key="withdraw-submit-fixture-retry",
            now=NOW + timedelta(seconds=2),
        )
    assert caught.value.code == "BINANCE_CAPITAL_SUBMISSION_UNKNOWN"
