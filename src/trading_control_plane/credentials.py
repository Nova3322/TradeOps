from __future__ import annotations

import base64
import binascii
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


@dataclass(frozen=True, slots=True)
class EncryptedCredentials:
    ciphertext: str
    metadata: dict[str, Any]


class CredentialCipher:
    """Versioned AES-256-GCM envelope for exchange credentials.

    The authenticated context binds a payload to one team, account, venue and
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
        nonce = secrets.token_bytes(NONCE_BYTES)
        plaintext = json.dumps(
            normalized,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode()
        ciphertext = self._aes().encrypt(
            nonce,
            plaintext,
            credential_aad(
                team_id=team_id,
                exchange_account_id=exchange_account_id,
                venue=venue,
                credential_version=credential_version,
            ),
        )
        return EncryptedCredentials(
            ciphertext=f"{ENVELOPE_VERSION}:"
            f"{base64.urlsafe_b64encode(nonce + ciphertext).decode().rstrip('=')}",
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
            plaintext = self._aes().decrypt(
                raw[:NONCE_BYTES],
                raw[NONCE_BYTES:],
                credential_aad(
                    team_id=team_id,
                    exchange_account_id=exchange_account_id,
                    venue=venue,
                    credential_version=credential_version,
                ),
            )
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
        return validate_exchange_credentials(venue, decoded)


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
