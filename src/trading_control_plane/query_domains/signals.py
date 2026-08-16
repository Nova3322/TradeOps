from __future__ import annotations

from trading_control_plane.query_component import QueryComponent

# ruff: noqa: F403, F405
from trading_control_plane.query_core import *


class SignalQueries(QueryComponent):
    def list_instruments(self, user_id: UUID) -> list[dict[str, Any]]:
        with self.database.session_factory() as session:
            user = session.get(User, user_id)
            if user is None or not user.active:
                raise DomainRejected("SESSION_REVOKED", "internal user is inactive or missing")
            assignments = session.scalars(
                select(RoleAssignment).where(RoleAssignment.user_id == user_id)
            ).all()
            values = session.scalars(
                select(Instrument)
                .where(
                    Instrument.active,
                    Instrument.collateral_currency.in_(("USDT", "USDC")),
                    Instrument.quote_currency == Instrument.collateral_currency,
                )
                .order_by(Instrument.venue, Instrument.symbol)
            ).all()
            return [
                {
                    "instrument_id": str(item.instrument_id),
                    "venue": item.venue,
                    "symbol": item.symbol,
                    "tick_size": str(item.tick_size),
                    "lot_size": str(item.lot_size),
                    "minimum_notional": str(item.minimum_notional),
                    "contract_multiplier": str(item.contract_multiplier),
                    "quote_currency": item.quote_currency,
                    "collateral_currency": item.collateral_currency,
                    "protection_supported": item.protection_supported,
                    "updated_at": _iso(item.updated_at),
                }
                for item in values
                if any(
                    assignment.venue_scope is None or assignment.venue_scope == item.venue
                    for assignment in assignments
                )
            ]

    def instrument_id_by_venue_symbol(self, venue: str, symbol: str) -> UUID:
        with self.database.session_factory() as session:
            instrument = session.scalar(
                select(Instrument).where(
                    Instrument.venue == venue,
                    Instrument.symbol == symbol,
                    Instrument.active,
                )
            )
            if instrument is None:
                raise DomainRejected(
                    "INSTRUMENT_UNAVAILABLE",
                    "candidate instrument is not active in the Trading catalog",
                )
            return instrument.instrument_id

    def active_instrument_keys(self, venue_symbols: set[tuple[str, str]]) -> set[tuple[str, str]]:
        """Return exact active Catalog matches without normalizing or guessing symbols."""

        if not venue_symbols:
            return set()
        with self.database.session_factory() as session:
            return set(
                session.execute(
                    select(Instrument.venue, Instrument.symbol).where(
                        Instrument.active,
                        tuple_(Instrument.venue, Instrument.symbol).in_(venue_symbols),
                    )
                ).tuples()
            )

    def compatible_legacy_system_candidate_id(
        self,
        legacy_candidate_id: str,
        candidate: PerptapeCandidate,
        instrument_id: UUID,
    ) -> str | None:
        """Reuse an exact legacy proposal identity without conflating quote contracts."""

        with self.database.session_factory() as session:
            proposal = session.scalar(
                select(Proposal).where(
                    Proposal.source == "SYSTEM",
                    Proposal.source_candidate_id == legacy_candidate_id,
                )
            )
            if (
                proposal is None
                or proposal.instrument_id != instrument_id
                or proposal.venue != candidate.venue
                or proposal.direction != candidate.direction.value
            ):
                return None
            details = proposal.frozen_payload.get("details")
            snapshot = details.get("candidate") if isinstance(details, dict) else None
            if not isinstance(snapshot, dict):
                return None
            current = candidate.to_dict()
            identity_fields = (
                "venue",
                "source_exchange",
                "symbol",
                "canonical_symbol",
                "direction",
                "source_direction",
                "timeframe",
                "triggered_at",
            )
            if any(snapshot.get(field) != current[field] for field in identity_fields):
                return None
            return legacy_candidate_id

    def perptape_feed(self, user_id: UUID) -> PerptapeFeedSnapshot | None:
        _workspace_id, team_id = self._active_scope_ids(user_id)
        with self.database.session_factory() as session:
            feed = session.get(PerptapeFeed, (team_id, "BREAKOUTS"))
            if feed is None:
                return None
            candidates: list[PerptapeCandidate] = []
            for value in feed.candidates:
                if not isinstance(value, dict):
                    raise DomainRejected(
                        "PERPTAPE_CACHE_INVALID",
                        "persisted Perptape feed contains an invalid candidate",
                    )
                candidates.append(PerptapeCandidate.from_dict(value))
            return PerptapeFeedSnapshot(
                contract_version=feed.contract_version,
                generated_at=feed.generated_at,
                fetched_at=feed.fetched_at,
                next_allowed_at=feed.next_allowed_at,
                candidates=tuple(candidates),
            )
