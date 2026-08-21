from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select, tuple_

from trading_control_plane import domain, models, perptape
from trading_control_plane.query_component import QueryComponent, iso_datetime


class SignalQueries(QueryComponent):
    def list_instruments(self, user_id: UUID) -> list[dict[str, Any]]:
        with self.database.session_factory() as session:
            user = session.get(models.User, user_id)
            if user is None or not user.active:
                raise domain.DomainRejected(
                    "SESSION_REVOKED", "internal user is inactive or missing"
                )
            assignments = session.scalars(
                select(models.RoleAssignment).where(models.RoleAssignment.user_id == user_id)
            ).all()
            values = session.scalars(
                select(models.Instrument)
                .where(
                    models.Instrument.active,
                    models.Instrument.collateral_currency.in_(("USDT", "USDC")),
                    models.Instrument.quote_currency == models.Instrument.collateral_currency,
                )
                .order_by(models.Instrument.venue, models.Instrument.symbol)
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
                    "updated_at": iso_datetime(item.updated_at),
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
                select(models.Instrument).where(
                    models.Instrument.venue == venue,
                    models.Instrument.symbol == symbol,
                    models.Instrument.active,
                )
            )
            if instrument is None:
                raise domain.DomainRejected(
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
                    select(models.Instrument.venue, models.Instrument.symbol).where(
                        models.Instrument.active,
                        tuple_(models.Instrument.venue, models.Instrument.symbol).in_(
                            venue_symbols
                        ),
                    )
                ).tuples()
            )

    def compatible_legacy_system_candidate_id(
        self,
        legacy_candidate_id: str,
        candidate: perptape.PerptapeCandidate,
        instrument_id: UUID,
    ) -> str | None:
        """Reuse an exact legacy proposal identity without conflating quote contracts."""

        with self.database.session_factory() as session:
            proposal = session.scalar(
                select(models.Proposal).where(
                    models.Proposal.source == "SYSTEM",
                    models.Proposal.source_candidate_id == legacy_candidate_id,
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

    @staticmethod
    def _perptape_snapshot(
        feed: models.PerptapeFeed,
        *,
        source_exchange: perptape.PerptapeSourceExchange | None = None,
    ) -> perptape.PerptapeFeedSnapshot:
        candidates: list[perptape.PerptapeCandidate] = []
        for value in feed.candidates:
            if not isinstance(value, dict):
                raise domain.DomainRejected(
                    "PERPTAPE_CACHE_INVALID",
                    "persisted Perptape feed contains an invalid candidate",
                )
            candidate = perptape.PerptapeCandidate.from_dict(value)
            if source_exchange is None or candidate.source_exchange == source_exchange:
                candidates.append(candidate)
        return perptape.PerptapeFeedSnapshot(
            contract_version=feed.contract_version,
            generated_at=feed.generated_at,
            fetched_at=feed.fetched_at,
            next_allowed_at=feed.next_allowed_at,
            candidates=tuple(candidates),
            source_exchange=source_exchange,
        )

    def perptape_feeds(
        self,
        user_id: UUID,
        *,
        include_legacy: bool = True,
    ) -> dict[perptape.PerptapeSourceExchange, perptape.PerptapeFeedSnapshot]:
        _workspace_id, team_id = self.active_scope_ids(user_id)
        with self.database.session_factory() as session:
            rows = {
                feed.feed_key: feed
                for feed in session.scalars(
                    select(models.PerptapeFeed).where(
                        models.PerptapeFeed.team_id == team_id,
                        models.PerptapeFeed.feed_key.in_(
                            (
                                perptape.PERPTAPE_LEGACY_FEED_KEY,
                                *perptape.PERPTAPE_FEED_KEYS.values(),
                            )
                        ),
                    )
                ).all()
            }
            result: dict[
                perptape.PerptapeSourceExchange,
                perptape.PerptapeFeedSnapshot,
            ] = {}
            for source_exchange in perptape.PERPTAPE_SOURCE_EXCHANGES:
                exact = rows.get(perptape.PERPTAPE_FEED_KEYS[source_exchange])
                if exact is not None:
                    result[source_exchange] = self._perptape_snapshot(
                        exact,
                        source_exchange=source_exchange,
                    )
            legacy = rows.get(perptape.PERPTAPE_LEGACY_FEED_KEY)
            if include_legacy and legacy is not None:
                for source_exchange in perptape.PERPTAPE_SOURCE_EXCHANGES:
                    if source_exchange in result:
                        continue
                    derived = self._perptape_snapshot(
                        legacy,
                        source_exchange=source_exchange,
                    )
                    if derived.candidates:
                        result[source_exchange] = derived
            return result

    def perptape_feed(
        self,
        user_id: UUID,
        *,
        feed_key: str | None = None,
    ) -> perptape.PerptapeFeedSnapshot | None:
        _workspace_id, team_id = self.active_scope_ids(user_id)
        if feed_key is not None:
            with self.database.session_factory() as session:
                feed = session.get(models.PerptapeFeed, (team_id, feed_key))
                if feed is None:
                    return None
                source_exchange = next(
                    (
                        source
                        for source, exact_key in perptape.PERPTAPE_FEED_KEYS.items()
                        if exact_key == feed_key
                    ),
                    None,
                )
                return self._perptape_snapshot(feed, source_exchange=source_exchange)
        feeds = self.perptape_feeds(user_id)
        if feeds:
            newest = max(feeds.values(), key=lambda value: value.fetched_at)
            return perptape.bound_perptape_feed_snapshot(
                perptape.PerptapeFeedSnapshot(
                    contract_version=newest.contract_version,
                    generated_at=max(feed.generated_at for feed in feeds.values()),
                    fetched_at=max(feed.fetched_at for feed in feeds.values()),
                    next_allowed_at=max(feed.next_allowed_at for feed in feeds.values()),
                    candidates=tuple(
                        candidate
                        for source_exchange in perptape.PERPTAPE_SOURCE_EXCHANGES
                        if source_exchange in feeds
                        for candidate in feeds[source_exchange].candidates
                    ),
                )
            )
        with self.database.session_factory() as session:
            legacy = session.get(
                models.PerptapeFeed,
                (team_id, perptape.PERPTAPE_LEGACY_FEED_KEY),
            )
            return None if legacy is None else self._perptape_snapshot(legacy)

    def perptape_polling_health(self, user_id: UUID) -> dict[str, dict[str, Any]]:
        _workspace_id, team_id = self.active_scope_ids(user_id)
        names = tuple(f"PERPTAPE:{source}" for source in perptape.PERPTAPE_SOURCE_EXCHANGES)
        with self.database.session_factory() as session:
            values = session.scalars(
                select(models.RuntimeSourceHealth).where(
                    models.RuntimeSourceHealth.team_id == team_id,
                    models.RuntimeSourceHealth.source_name.in_(names),
                    models.RuntimeSourceHealth.account_id.is_(None),
                    models.RuntimeSourceHealth.venue.is_(None),
                )
            ).all()
            return {
                item.source_name.removeprefix("PERPTAPE:"): {
                    "status": item.status,
                    "items_observed": item.items_observed,
                    "error_code": item.error_code,
                    "checked_at": item.checked_at,
                    "last_success_at": item.last_success_at,
                    "retry_at": item.retry_at,
                    "consecutive_failures": item.consecutive_failures,
                }
                for item in values
            }
