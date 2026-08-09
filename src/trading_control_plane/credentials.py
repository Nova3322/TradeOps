from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from trading_control_plane.domain import DomainRejected

ENVELOPE_VERSION = "v1"
NONCE_BYTES = 12
SUPPORTED_EXCHANGE_VENUES = frozenset({"BINANCE", "HYPERLIQUID", "OKX", "BYBIT"})
EVM_ADDRESS_PATTERN = re.compile(r"^0x[0-9a-fA-F]{40}$")


def _decode_key(value: str) -> bytes:
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, binascii.Error) as exc:
        raise DomainRejected(
            "CREDENTIAL_ENCRYPTION_KEY_INVALID",
            "credential encryption key must be URL-safe base64",
        ) from exc
    if len(decoded) != 32:
        raise DomainRejected(
            "CREDENTIAL_ENCRYPTION_KEY_INVALID",
            "credential encryption key must decode to exactly 32 bytes",
        )
    return decoded


def credential_aad(
    *,
    team_id: UUID,
    exchange_account_id: UUID,
    venue: str,
    credential_version: int,
) -> bytes:
    return (
        f"tradingops:exchange-account:{team_id}:{exchange_account_id}:{venue}:{credential_version}"
    ).encode()


def scoped_secret_aad(
    *,
    team_id: UUID,
    object_id: UUID,
    purpose: str,
    credential_version: int,
) -> bytes:
    return (
        f"tradingops:{purpose}:{team_id}:{object_id}:{credential_version}"
    ).encode()


@dataclass(frozen=True, slots=True)
class EncryptedCredentials:
    ciphertext: str
    metadata: dict[str, Any]


class CredentialCipher:
    """Versioned AES-256-GCM envelope for scoped operational secrets.

    The authenticated context binds a payload to one team, object, purpose and
    rotation version. Decryption is deliberately separate from API projections.
    """

    def __init__(self, key: str | None) -> None:
        self._key = None if key is None else _decode_key(key)

    def _aes(self) -> AESGCM:
        if self._key is None:
            raise DomainRejected(
                "CREDENTIAL_ENCRYPTION_KEY_MISSING",
                "credential writes require TRADING_CREDENTIAL_ENCRYPTION_KEY",
            )
        return AESGCM(self._key)

    def secret_fingerprint(self, secret: str, *, purpose: str) -> str:
        if self._key is None:
            raise DomainRejected(
                "CREDENTIAL_ENCRYPTION_KEY_MISSING",
                "credential writes require TRADING_CREDENTIAL_ENCRYPTION_KEY",
            )
        fingerprint_key = hmac.new(
            self._key,
            b"tradingops:secret-fingerprint:v1",
            hashlib.sha256,
        ).digest()
        return hmac.new(
            fingerprint_key,
            purpose.encode() + b"\x00" + secret.encode(),
            hashlib.sha256,
        ).hexdigest()

    def exchange_credentials_fingerprint(
        self,
        credentials: dict[str, str],
        *,
        venue: str,
        purpose: str,
    ) -> str:
        """Keyed idempotency material that cannot be used to guess stored credentials."""

        normalized = validate_exchange_credentials(venue, credentials)
        canonical = json.dumps(
            normalized,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        return self.secret_fingerprint(canonical, purpose=purpose)

    def _encrypt_payload(
        self,
        payload: dict[str, str],
        *,
        aad: bytes,
        metadata: dict[str, Any],
    ) -> EncryptedCredentials:
        nonce = secrets.token_bytes(NONCE_BYTES)
        plaintext = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode()
        ciphertext = self._aes().encrypt(nonce, plaintext, aad)
        return EncryptedCredentials(
            ciphertext=f"{ENVELOPE_VERSION}:"
            f"{base64.urlsafe_b64encode(nonce + ciphertext).decode().rstrip('=')}",
            metadata=metadata,
        )

    def _decrypt_payload(self, envelope: str, *, aad: bytes) -> dict[str, str]:
        version, separator, payload = envelope.partition(":")
        if separator != ":" or version != ENVELOPE_VERSION:
            raise DomainRejected(
                "CREDENTIAL_ENVELOPE_UNSUPPORTED",
                "credential envelope version is unsupported",
            )
        try:
            raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        except (ValueError, binascii.Error) as exc:
            raise DomainRejected(
                "CREDENTIAL_ENVELOPE_INVALID", "credential envelope is malformed"
            ) from exc
        if len(raw) <= NONCE_BYTES:
            raise DomainRejected("CREDENTIAL_ENVELOPE_INVALID", "credential envelope is malformed")
        try:
            plaintext = self._aes().decrypt(raw[:NONCE_BYTES], raw[NONCE_BYTES:], aad)
        except InvalidTag as exc:
            raise DomainRejected(
                "CREDENTIAL_AUTHENTICATION_FAILED",
                "credential envelope authentication failed",
            ) from exc
        try:
            decoded = json.loads(plaintext)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DomainRejected(
                "CREDENTIAL_ENVELOPE_INVALID", "credential envelope payload is invalid"
            ) from exc
        if not isinstance(decoded, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in decoded.items()
        ):
            raise DomainRejected(
                "CREDENTIAL_ENVELOPE_INVALID", "credential envelope payload is invalid"
            )
        return decoded

    def encrypt(
        self,
        credentials: dict[str, str],
        *,
        team_id: UUID,
        exchange_account_id: UUID,
        venue: str,
        credential_version: int,
    ) -> EncryptedCredentials:
        normalized = validate_exchange_credentials(venue, credentials)
        return self._encrypt_payload(
            normalized,
            aad=credential_aad(
                team_id=team_id,
                exchange_account_id=exchange_account_id,
                venue=venue,
                credential_version=credential_version,
            ),
            metadata=credential_metadata(venue, normalized),
        )

    def decrypt(
        self,
        envelope: str,
        *,
        team_id: UUID,
        exchange_account_id: UUID,
        venue: str,
        credential_version: int,
    ) -> dict[str, str]:
        decoded = self._decrypt_payload(
            envelope,
            aad=credential_aad(
                team_id=team_id,
                exchange_account_id=exchange_account_id,
                venue=venue,
                credential_version=credential_version,
            ),
        )
        return validate_exchange_credentials(venue, decoded)

    def encrypt_secret(
        self,
        secret: str,
        *,
        team_id: UUID,
        object_id: UUID,
        purpose: str,
        credential_version: int,
    ) -> EncryptedCredentials:
        normalized = secret.strip()
        if not normalized or normalized != secret:
            raise DomainRejected(
                "SCOPED_SECRET_INVALID",
                "operational secret must be non-empty without surrounding whitespace",
            )
        return self._encrypt_payload(
            {"secret": normalized},
            aad=scoped_secret_aad(
                team_id=team_id,
                object_id=object_id,
                purpose=purpose,
                credential_version=credential_version,
            ),
            metadata={
                "envelope_version": ENVELOPE_VERSION,
                "purpose": purpose,
                "key_hint": f"••••{normalized[-4:]}",
            },
        )

    def decrypt_secret(
        self,
        envelope: str,
        *,
        team_id: UUID,
        object_id: UUID,
        purpose: str,
        credential_version: int,
    ) -> str:
        decoded = self._decrypt_payload(
            envelope,
            aad=scoped_secret_aad(
                team_id=team_id,
                object_id=object_id,
                purpose=purpose,
                credential_version=credential_version,
            ),
        )
        if not isinstance(decoded, dict) or set(decoded) != {"secret"} or not isinstance(
            decoded["secret"], str
        ):
            raise DomainRejected(
                "CREDENTIAL_ENVELOPE_INVALID", "credential envelope payload is invalid"
            )
        return decoded["secret"]


def validate_exchange_credentials(venue: str, credentials: dict[str, str]) -> dict[str, str]:
    normalized_venue = venue.upper()
    if normalized_venue not in SUPPORTED_EXCHANGE_VENUES:
        raise DomainRejected("EXCHANGE_VENUE_UNSUPPORTED", "exchange venue is unsupported")
    normalized = {
        key: value.strip()
        for key, value in credentials.items()
        if isinstance(value, str) and value.strip()
    }
    allowed: dict[str, frozenset[str]] = {
        "BINANCE": frozenset({"api_key", "api_secret"}),
        "OKX": frozenset({"api_key", "api_secret", "passphrase"}),
        "BYBIT": frozenset({"api_key", "api_secret"}),
        "HYPERLIQUID": frozenset(
            {"account_address", "api_wallet_address", "api_wallet_private_key"}
        ),
    }
    required: dict[str, frozenset[str]] = {
        "BINANCE": frozenset({"api_key", "api_secret"}),
        "OKX": frozenset({"api_key", "api_secret", "passphrase"}),
        "BYBIT": frozenset({"api_key", "api_secret"}),
        "HYPERLIQUID": frozenset({"account_address"}),
    }
    unknown = set(normalized) - allowed[normalized_venue]
    missing = required[normalized_venue] - set(normalized)
    if unknown or missing:
        raise DomainRejected(
            "EXCHANGE_CREDENTIALS_INVALID",
            "credential fields do not match the selected exchange",
        )
    if normalized_venue == "HYPERLIQUID":
        for field in ("account_address", "api_wallet_address"):
            value = normalized.get(field)
            if value is not None and not EVM_ADDRESS_PATTERN.fullmatch(value):
                raise DomainRejected(
                    "EXCHANGE_CREDENTIALS_INVALID",
                    "Hyperliquid account addresses must be 20-byte EVM addresses",
                )
        if "api_wallet_private_key" in normalized and "api_wallet_address" not in normalized:
            raise DomainRejected(
                "EXCHANGE_CREDENTIALS_INVALID",
                "a Hyperliquid API wallet private key requires its public wallet address",
            )
    return normalized


def credential_metadata(venue: str, credentials: dict[str, str]) -> dict[str, Any]:
    public_hint_source = credentials.get("api_key") or credentials.get("api_wallet_address")
    return {
        "envelope_version": ENVELOPE_VERSION,
        "configured_fields": sorted(credentials),
        "key_hint": None if public_hint_source is None else f"••••{public_hint_source[-4:]}",
        "signing_material_configured": any(
            field in credentials for field in ("api_secret", "passphrase", "api_wallet_private_key")
        ),
        "venue": venue.upper(),
    }
