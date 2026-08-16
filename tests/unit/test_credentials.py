from __future__ import annotations

import base64
import hashlib
import secrets
from uuid import uuid4

import pytest

from trading_control_plane.credentials import CredentialCipher
from trading_control_plane.domain import DomainRejected


def key() -> str:
    return base64.urlsafe_b64encode(b"k" * 32).decode().rstrip("=")


def test_credentials_round_trip_and_metadata_never_contains_secret() -> None:
    team_id = uuid4()
    account_id = uuid4()
    cipher = CredentialCipher(key())
    encrypted = cipher.encrypt(
        {"api_key": "public-key-1234", "api_secret": "must-not-leak"},
        team_id=team_id,
        exchange_account_id=account_id,
        venue="BINANCE",
        credential_version=1,
    )

    assert "must-not-leak" not in encrypted.ciphertext
    assert "must-not-leak" not in repr(encrypted.metadata)
    assert encrypted.metadata == {
        "envelope_version": "v1",
        "configured_fields": ["api_key", "api_secret"],
        "key_hint": "••••1234",
        "signing_material_configured": True,
        "venue": "BINANCE",
    }
    assert cipher.decrypt(
        encrypted.ciphertext,
        team_id=team_id,
        exchange_account_id=account_id,
        venue="BINANCE",
        credential_version=1,
    ) == {"api_key": "public-key-1234", "api_secret": "must-not-leak"}


def test_authenticated_context_blocks_cross_team_or_version_decryption() -> None:
    team_id = uuid4()
    account_id = uuid4()
    cipher = CredentialCipher(key())
    encrypted = cipher.encrypt(
        {"api_key": "key", "api_secret": "secret"},
        team_id=team_id,
        exchange_account_id=account_id,
        venue="BYBIT",
        credential_version=1,
    )

    with pytest.raises(DomainRejected, match="CREDENTIAL_AUTHENTICATION_FAILED"):
        cipher.decrypt(
            encrypted.ciphertext,
            team_id=uuid4(),
            exchange_account_id=account_id,
            venue="BYBIT",
            credential_version=1,
        )


def test_scoped_secret_round_trip_and_authenticated_context() -> None:
    team_id = uuid4()
    object_id = uuid4()
    secret = secrets.token_urlsafe(32)
    cipher = CredentialCipher(key())
    encrypted = cipher.encrypt_secret(
        secret,
        team_id=team_id,
        object_id=object_id,
        purpose="team-signal-source",
        credential_version=2,
    )
    fingerprint = cipher.secret_fingerprint(secret, purpose="team-signal-source")

    assert secret not in encrypted.ciphertext
    assert secret not in repr(encrypted.metadata)
    assert secret not in fingerprint
    assert fingerprint == cipher.secret_fingerprint(secret, purpose="team-signal-source")
    assert fingerprint != cipher.secret_fingerprint(secret, purpose="another-team-signal-source")
    assert encrypted.metadata == {
        "envelope_version": "v1",
        "purpose": "team-signal-source",
        "key_hint": f"••••{secret[-4:]}",
    }
    assert (
        cipher.decrypt_secret(
            encrypted.ciphertext,
            team_id=team_id,
            object_id=object_id,
            purpose="team-signal-source",
            credential_version=2,
        )
        == secret
    )
    with pytest.raises(DomainRejected, match="CREDENTIAL_AUTHENTICATION_FAILED"):
        cipher.decrypt_secret(
            encrypted.ciphertext,
            team_id=uuid4(),
            object_id=object_id,
            purpose="team-signal-source",
            credential_version=2,
        )


def test_exchange_credential_fingerprint_is_keyed_and_purpose_bound() -> None:
    credentials = {"api_key": "key", "api_secret": "guessable-secret"}
    cipher = CredentialCipher(key())
    fingerprint = cipher.exchange_credentials_fingerprint(
        credentials,
        venue="BINANCE",
        purpose="exchange-account.create",
    )

    assert (
        fingerprint
        != hashlib.sha256(b'{"api_key":"key","api_secret":"guessable-secret"}').hexdigest()
    )
    assert fingerprint == cipher.exchange_credentials_fingerprint(
        credentials,
        venue="BINANCE",
        purpose="exchange-account.create",
    )
    assert fingerprint != cipher.exchange_credentials_fingerprint(
        credentials,
        venue="BINANCE",
        purpose="exchange-account.credentials.rotate",
    )


def test_missing_key_and_invalid_venue_credentials_fail_closed() -> None:
    with pytest.raises(DomainRejected, match="CREDENTIAL_ENCRYPTION_KEY_MISSING"):
        CredentialCipher(None).encrypt(
            {"api_key": "key", "api_secret": "secret"},
            team_id=uuid4(),
            exchange_account_id=uuid4(),
            venue="BINANCE",
            credential_version=1,
        )
    with pytest.raises(DomainRejected, match="EXCHANGE_CREDENTIALS_INVALID"):
        CredentialCipher(key()).encrypt(
            {"api_key": "key", "api_secret": "secret"},
            team_id=uuid4(),
            exchange_account_id=uuid4(),
            venue="OKX",
            credential_version=1,
        )
