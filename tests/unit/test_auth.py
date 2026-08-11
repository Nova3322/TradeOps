from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from trading_control_plane.auth import SignedTokenService
from trading_control_plane.domain import DomainRejected
from trading_control_plane.passwords import (
    ApiClientRateLimiter,
    LoginAttemptLimiter,
    PasswordHasher,
)

NOW = datetime(2026, 7, 19, 8, tzinfo=UTC)
USER_ID = UUID("00000000-0000-0000-0000-000000000001")
OBJECT_ID = UUID("00000000-0000-0000-0000-000000000002")


def signer() -> SignedTokenService:
    return SignedTokenService("test-signing-secret-that-is-long-enough")


def test_signed_session_round_trip_and_expiry() -> None:
    token = signer().issue_session(
        user_id=USER_ID,
        username="reviewer",
        now=NOW,
        ttl=timedelta(minutes=5),
        authentication_method="managed-passkey",
        auth_version=3,
    )

    identity = signer().verify_session(token, now=NOW + timedelta(minutes=1))

    assert identity.user_id == USER_ID
    assert identity.username == "reviewer"
    assert identity.authentication_method == "managed-passkey"
    assert identity.auth_version == 3
    with pytest.raises(DomainRejected, match="SESSION_EXPIRED"):
        signer().verify_session(token, now=NOW + timedelta(minutes=5))


def test_tampered_session_is_rejected() -> None:
    token = signer().issue_session(
        user_id=USER_ID,
        username="reviewer",
        now=NOW,
        ttl=timedelta(minutes=5),
        authentication_method="managed-passkey",
    )

    with pytest.raises(DomainRejected, match="AUTH_TOKEN_INVALID"):
        signer().verify_session(token + "tampered", now=NOW)


def test_action_grant_is_bound_to_user_action_object_and_version() -> None:
    token = signer().issue_action_grant(
        user_id=USER_ID,
        action="proposal.approve",
        object_id=OBJECT_ID,
        object_version=2,
        now=NOW,
        ttl=timedelta(minutes=5),
        authentication_method="passkey",
    )

    grant = signer().verify_action_grant(
        token,
        user_id=USER_ID,
        action="proposal.approve",
        object_id=OBJECT_ID,
        object_version=2,
        now=NOW + timedelta(minutes=1),
    )

    assert grant.authentication_method == "passkey"
    with pytest.raises(DomainRejected, match="ACTION_GRANT_SCOPE_INVALID"):
        signer().verify_action_grant(
            token,
            user_id=USER_ID,
            action="proposal.approve",
            object_id=OBJECT_ID,
            object_version=3,
            now=NOW,
        )


def test_telegram_review_reference_cannot_be_used_as_approval_grant() -> None:
    reference = signer().issue_review_reference(
        user_id=USER_ID,
        object_id=OBJECT_ID,
        object_version=2,
        now=NOW,
        ttl=timedelta(minutes=5),
    )

    with pytest.raises(DomainRejected, match="ACTION_GRANT_SCOPE_INVALID"):
        signer().verify_action_grant(
            reference,
            user_id=USER_ID,
            action="proposal.approve",
            object_id=OBJECT_ID,
            object_version=2,
            now=NOW,
        )


def test_password_hash_is_salted_and_verifies_without_plaintext() -> None:
    hasher = PasswordHasher()

    first = hasher.hash("correct horse battery staple")
    second = hasher.hash("correct horse battery staple")

    assert first != second
    assert "correct horse" not in first
    assert hasher.verify("correct horse battery staple", first) is True
    assert hasher.verify("wrong password value", first) is False
    assert hasher.verify("correct horse battery staple", "invalid-hash") is False


def test_password_policy_requires_a_real_secret() -> None:
    with pytest.raises(DomainRejected, match="PASSWORD_INVALID"):
        PasswordHasher().hash("too-short")


def test_login_attempt_limiter_locks_and_clears_a_credential_scope() -> None:
    limiter = LoginAttemptLimiter(max_attempts=2, lockout=timedelta(minutes=2))
    key = "127.0.0.1:kelly_oooo"

    assert limiter.fail(key, now=NOW) is None
    assert limiter.fail(key, now=NOW + timedelta(seconds=1)) == 120
    assert limiter.retry_after(key, now=NOW + timedelta(seconds=2)) == 119
    limiter.success(key)
    assert limiter.retry_after(key, now=NOW + timedelta(seconds=2)) is None


def test_api_client_rate_limit_is_isolated_by_client_and_recovers_after_window() -> None:
    limiter = ApiClientRateLimiter(max_requests=2, window=timedelta(minutes=1))

    assert limiter.consume("client-a", now=NOW) is None
    assert limiter.consume("client-a", now=NOW + timedelta(seconds=1)) is None
    assert limiter.consume("client-a", now=NOW + timedelta(seconds=2)) == 58
    assert limiter.consume("client-b", now=NOW + timedelta(seconds=2)) is None
    assert limiter.consume("client-a", now=NOW + timedelta(seconds=61)) is None
