#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import delete, func, select

from trading_control_plane.config import get_settings
from trading_control_plane.database import Database
from trading_control_plane.models import (
    AnalyticsEquitySnapshot,
    ExchangeAccount,
    ShadowFill,
    ShadowInstrument,
    ShadowOrder,
    ShadowPosition,
    Team,
    TeamShadowAccount,
    User,
)

FIXTURE_ID = "TRADINGOPS_SHADOW_ANALYTICS_V1"
SYMBOL = "FIXTUREBTCUSDT"
DAYS = 120


def fixture_uuid(team_id: UUID, generation: int, label: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"{FIXTURE_ID}:{team_id}:{generation}:{label}")


def resolve_scope(
    session: object, username: str
) -> tuple[User, Team, TeamShadowAccount, ExchangeAccount]:
    user = session.scalar(select(User).where(User.username == username))
    if user is None or user.active_team_id is None:
        raise RuntimeError("FIXTURE_USER_SCOPE_MISSING: user or active Team is unavailable")
    team = session.get(Team, user.active_team_id)
    if team is None or team.execution_mode != "SHADOW":
        raise RuntimeError("FIXTURE_SHADOW_REQUIRED: active Team must be in SHADOW mode")
    shadow_account = session.scalar(
        select(TeamShadowAccount).where(
            TeamShadowAccount.team_id == team.team_id,
            TeamShadowAccount.status == "ACTIVE",
        )
    )
    account = session.scalar(
        select(ExchangeAccount)
        .where(ExchangeAccount.team_id == team.team_id, ExchangeAccount.active)
        .order_by(ExchangeAccount.venue, ExchangeAccount.account_id)
    )
    if shadow_account is None or account is None:
        raise RuntimeError("FIXTURE_SCOPE_INCOMPLETE: shadow and source accounts are required")
    return user, team, shadow_account, account


def cleanup(session: object, team: Team, shadow_account: TeamShadowAccount) -> dict[str, int]:
    prefix = f"{FIXTURE_ID}:g{shadow_account.generation}:"
    instrument = session.scalar(
        select(ShadowInstrument).where(
            ShadowInstrument.team_id == team.team_id,
            ShadowInstrument.symbol == SYMBOL,
        )
    )
    baseline = session.scalar(
        select(AnalyticsEquitySnapshot)
        .where(
            AnalyticsEquitySnapshot.team_id == team.team_id,
            AnalyticsEquitySnapshot.environment == "SHADOW",
            AnalyticsEquitySnapshot.generation == shadow_account.generation,
            AnalyticsEquitySnapshot.source_kind == FIXTURE_ID,
        )
        .order_by(AnalyticsEquitySnapshot.observed_at)
    )
    order_ids = list(
        session.scalars(
            select(ShadowOrder.shadow_order_id).where(
                ShadowOrder.team_id == team.team_id,
                ShadowOrder.generation == shadow_account.generation,
                ShadowOrder.idempotency_key.startswith(prefix),
            )
        )
    )
    fill_count = 0
    order_count = len(order_ids)
    position_count = 0
    if order_ids:
        fill_count = session.execute(
            delete(ShadowFill).where(ShadowFill.shadow_order_id.in_(order_ids))
        ).rowcount
        session.execute(delete(ShadowOrder).where(ShadowOrder.shadow_order_id.in_(order_ids)))
    if instrument is not None:
        position_count = session.execute(
            delete(ShadowPosition).where(
                ShadowPosition.shadow_instrument_id == instrument.shadow_instrument_id,
                ShadowPosition.generation == shadow_account.generation,
            )
        ).rowcount
        remaining_orders = session.scalar(
            select(func.count(ShadowOrder.shadow_order_id)).where(
                ShadowOrder.shadow_instrument_id == instrument.shadow_instrument_id
            )
        )
        remaining_positions = session.scalar(
            select(func.count(ShadowPosition.shadow_position_id)).where(
                ShadowPosition.shadow_instrument_id == instrument.shadow_instrument_id
            )
        )
        if not remaining_orders and not remaining_positions:
            session.delete(instrument)
    snapshot_count = session.execute(
        delete(AnalyticsEquitySnapshot).where(
            AnalyticsEquitySnapshot.team_id == team.team_id,
            AnalyticsEquitySnapshot.environment == "SHADOW",
            AnalyticsEquitySnapshot.generation == shadow_account.generation,
            AnalyticsEquitySnapshot.source_kind == FIXTURE_ID,
        )
    ).rowcount
    if baseline is not None:
        stored = baseline.fact_metadata.get("baseline_shadow_account", {})
        for field in (
            "equity",
            "available_balance",
            "realized_pnl",
            "unrealized_pnl",
            "fees_paid",
        ):
            if field in stored:
                setattr(shadow_account, field, Decimal(stored[field]))
        shadow_account.version += 1
        shadow_account.updated_at = datetime.now(UTC)
    return {
        "snapshots_deleted": snapshot_count,
        "orders_deleted": order_count,
        "fills_deleted": fill_count,
        "positions_deleted": position_count,
    }


def seed(
    session: object,
    team: Team,
    shadow_account: TeamShadowAccount,
    account: ExchangeAccount,
) -> dict[str, object]:
    existing = session.scalar(
        select(AnalyticsEquitySnapshot.snapshot_id).where(
            AnalyticsEquitySnapshot.team_id == team.team_id,
            AnalyticsEquitySnapshot.environment == "SHADOW",
            AnalyticsEquitySnapshot.generation == shadow_account.generation,
            AnalyticsEquitySnapshot.source_kind == FIXTURE_ID,
        )
    )
    if existing is not None:
        return {
            "fixture_id": FIXTURE_ID,
            "status": "ALREADY_PRESENT",
            "generation": shadow_account.generation,
        }

    now = datetime.now(UTC)
    start = datetime.combine((now - timedelta(days=DAYS)).date(), time(23, 0), UTC)
    baseline = {
        field: str(getattr(shadow_account, field))
        for field in (
            "equity",
            "available_balance",
            "realized_pnl",
            "unrealized_pnl",
            "fees_paid",
        )
    }
    nav = Decimal("100000.00")
    pattern = (24, 31, -18, 42, -71, -54, 38, 27, -12, 46, 35, -88, 64, 52, -22, 29)
    for day in range(DAYS + 1):
        if day:
            daily_return = Decimal(pattern[(day - 1) % len(pattern)]) / Decimal(10000)
            nav = (nav * (Decimal(1) + daily_return)).quantize(Decimal("0.01"))
        observed_at = start + timedelta(days=day)
        session.add(
            AnalyticsEquitySnapshot(
                snapshot_id=fixture_uuid(team.team_id, shadow_account.generation, f"nav:{day}"),
                team_id=team.team_id,
                environment="SHADOW",
                account_id="TEAM_SHADOW",
                venue="TRADINGOPS",
                generation=shadow_account.generation,
                equity=nav,
                currency="U",
                source_kind=FIXTURE_ID,
                source_id=f"{FIXTURE_ID}:g{shadow_account.generation}:nav:{day:03d}",
                version=1,
                fact_metadata={
                    "fixture": True,
                    "fixture_id": FIXTURE_ID,
                    "deterministic_pattern_bps": list(pattern),
                    "baseline_shadow_account": baseline if day == 0 else {},
                },
                observed_at=observed_at,
                recorded_at=now,
            )
        )

    instrument = session.scalar(
        select(ShadowInstrument).where(
            ShadowInstrument.team_id == team.team_id,
            ShadowInstrument.venue == account.venue,
            ShadowInstrument.symbol == SYMBOL,
        )
    )
    if instrument is None:
        instrument = ShadowInstrument(
            shadow_instrument_id=fixture_uuid(team.team_id, 0, "instrument"),
            team_id=team.team_id,
            catalog_instrument_id=None,
            venue=account.venue,
            symbol=SYMBOL,
            price_tick=Decimal("0.1"),
            quantity_step=Decimal("0.001"),
            contract_multiplier=Decimal("1"),
            is_derivative=True,
            latest_price=Decimal("30000"),
            price_observed_at=now,
            version=1,
            created_at=start,
            updated_at=now,
        )
        session.add(instrument)
        session.flush([instrument])
    total_fees = Decimal(0)
    total_realized = Decimal(0)
    order_count = 0
    for cycle in range(12):
        direction = "LONG" if cycle % 2 == 0 else "SHORT"
        entry_side = "BUY" if direction == "LONG" else "SELL"
        exit_side = "SELL" if direction == "LONG" else "BUY"
        entry = Decimal(28000 + cycle * 350)
        delta = Decimal(520 if cycle % 3 != 1 else -430)
        exit_price = entry + delta if direction == "LONG" else entry - delta
        quantity = Decimal("0.12") + Decimal(cycle % 4) * Decimal("0.03")
        realized = (
            (exit_price - entry) * quantity
            if direction == "LONG"
            else (entry - exit_price) * quantity
        )
        for leg, side, price, realized_pnl in (
            ("open", entry_side, entry, Decimal(0)),
            ("close", exit_side, exit_price, realized),
        ):
            executed_at = start + timedelta(
                days=cycle * 10 + (1 if leg == "open" else 7),
                hours=cycle % 5,
            )
            notional = (price * quantity).copy_abs()
            fee = (notional * Decimal("0.0004")).quantize(Decimal("0.00000001"))
            total_fees += fee
            total_realized += realized_pnl
            order_id = fixture_uuid(team.team_id, shadow_account.generation, f"order:{cycle}:{leg}")
            order = ShadowOrder(
                shadow_order_id=order_id,
                shadow_account_id=shadow_account.shadow_account_id,
                team_id=team.team_id,
                generation=shadow_account.generation,
                shadow_instrument_id=instrument.shadow_instrument_id,
                source_account_id=account.account_id,
                venue=account.venue,
                campaign_id=None,
                order_intent_id=None,
                shadow_position_id=None,
                side=side,
                order_type="MARKET",
                quantity=quantity,
                limit_price=None,
                trigger_price=None,
                trigger_type=None,
                execution_type=None,
                reduce_only=leg == "close",
                status="FILLED",
                filled_quantity=quantity,
                fill_price=price,
                fee=fee,
                realized_pnl=realized_pnl,
                correlation_id=fixture_uuid(
                    team.team_id,
                    shadow_account.generation,
                    f"correlation:{cycle}:{leg}",
                ),
                idempotency_key=f"{FIXTURE_ID}:g{shadow_account.generation}:order:{cycle:02d}:{leg}",
                version=1,
                created_at=executed_at,
                updated_at=executed_at,
            )
            session.add(order)
            session.flush([order])
            session.add(
                ShadowFill(
                    shadow_fill_id=fixture_uuid(
                        team.team_id,
                        shadow_account.generation,
                        f"fill:{cycle}:{leg}",
                    ),
                    shadow_order_id=order_id,
                    shadow_account_id=shadow_account.shadow_account_id,
                    team_id=team.team_id,
                    generation=shadow_account.generation,
                    shadow_instrument_id=instrument.shadow_instrument_id,
                    side=side,
                    quantity=quantity,
                    price=price,
                    notional=notional,
                    fee=fee,
                    realized_pnl=realized_pnl,
                    executed_at=executed_at,
                )
            )
            order_count += 1

    open_quantity = Decimal("0.08")
    open_entry = Decimal("30000")
    mark = Decimal("30400")
    unrealized = (mark - open_entry) * open_quantity
    position = ShadowPosition(
        shadow_position_id=fixture_uuid(team.team_id, shadow_account.generation, "open-position"),
        shadow_account_id=shadow_account.shadow_account_id,
        team_id=team.team_id,
        generation=shadow_account.generation,
        shadow_instrument_id=instrument.shadow_instrument_id,
        source_account_id=account.account_id,
        venue=account.venue,
        quantity=open_quantity,
        average_entry_price=open_entry,
        mark_price=mark,
        realized_pnl=total_realized,
        unrealized_pnl=unrealized,
        status="OPEN",
        version=1,
        created_at=start + timedelta(days=119),
        updated_at=start + timedelta(days=120),
    )
    session.add(position)
    shadow_account.equity = nav
    shadow_account.available_balance = (
        nav - abs(mark * open_quantity) * Decimal("0.1")
    ).quantize(Decimal("0.01"))
    shadow_account.realized_pnl = total_realized
    shadow_account.unrealized_pnl = unrealized
    shadow_account.fees_paid = total_fees
    shadow_account.version += 1
    shadow_account.updated_at = now
    return {
        "fixture_id": FIXTURE_ID,
        "status": "CREATED",
        "generation": shadow_account.generation,
        "nav_points": DAYS + 1,
        "return_points": DAYS,
        "orders": order_count,
        "fills": order_count,
        "positions": 1,
        "from_time": start.isoformat(),
        "to_time": (start + timedelta(days=DAYS)).isoformat(),
        "final_equity": str(nav),
        "fees": str(total_fees),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seed or clean deterministic local SHADOW analytics history"
    )
    parser.add_argument("--username", required=True)
    parser.add_argument("--cleanup", action="store_true")
    args = parser.parse_args()
    settings = get_settings()
    if settings.environment not in {"local", "test"}:
        raise RuntimeError("FIXTURE_LOCAL_ONLY: TRADING_ENVIRONMENT must be local or test")
    database = Database(settings.database_url)
    with database.session_factory.begin() as session:
        _user, team, shadow_account, account = resolve_scope(session, args.username)
        result = (
            cleanup(session, team, shadow_account)
            if args.cleanup
            else seed(session, team, shadow_account, account)
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
