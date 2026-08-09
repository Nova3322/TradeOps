from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from trading_control_plane.domain import DomainRejected


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


@dataclass(frozen=True)
class SessionIdentity:
    user_id: UUID
    username: str
    expires_at: datetime
    authentication_method: str
    auth_version: int = 1


@dataclass(frozen=True)
class ActionGrant:
    user_id: UUID
    action: str
    object_id: UUID
    object_version: int
    expires_at: datetime
    authentication_method: str


@dataclass(frozen=True)
class ActionReference:
    user_id: UUID
    action: str
    object_id: UUID
    object_version: int
    expires_at: datetime


class SignedTokenService:
    """Small signed-token boundary used by non-production identity and future IdP callbacks."""

    def __init__(self, secret: str) -> None:
        self._secret = secret.encode("utf-8")

    def _sign(self, payload: dict[str, Any]) -> str:
        body = _encode(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
        signature = _encode(hmac.new(self._secret, body.encode(), hashlib.sha256).digest())
        return f"{body}.{signature}"

    def _verify(self, token: str) -> dict[str, Any]:
        try:
            body, signature = token.split(".", 1)
            expected = _encode(hmac.new(self._secret, body.encode(), hashlib.sha256).digest())
            if not hmac.compare_digest(signature, expected):
                raise ValueError
            value = json.loads(_decode(body))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DomainRejected("AUTH_TOKEN_INVALID", "authentication token is invalid") from exc
        if not isinstance(value, dict):
            raise DomainRejected("AUTH_TOKEN_INVALID", "authentication token is invalid")
        return value

    def issue_session(
        self,
        *,
        user_id: UUID,
        username: str,
        now: datetime,
        ttl: timedelta,
        authentication_method: str,
        auth_version: int = 1,
    ) -> str:
        expires_at = now + ttl
        return self._sign(
            {
                "kind": "session",
                "sub": str(user_id),
                "username": username,
                "exp": int(expires_at.timestamp()),
                "amr": authentication_method,
                "auth_version": auth_version,
            }
        )

    def verify_session(self, token: str, *, now: datetime) -> SessionIdentity:
        payload = self._verify(token)
        if payload.get("kind") != "session":
            raise DomainRejected("AUTH_TOKEN_INVALID", "token is not a login session")
        expires_at = datetime.fromtimestamp(int(payload.get("exp", 0)), tz=UTC)
        if expires_at <= now:
            raise DomainRejected("SESSION_EXPIRED", "login session has expired")
        try:
            return SessionIdentity(
                user_id=UUID(str(payload["sub"])),
                username=str(payload["username"]),
                expires_at=expires_at,
                authentication_method=str(payload["amr"]),
                auth_version=int(payload.get("auth_version", 1)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise DomainRejected("AUTH_TOKEN_INVALID", "session claims are invalid") from exc

    def issue_action_grant(
        self,
        *,
        user_id: UUID,
        action: str,
        object_id: UUID,
        object_version: int,
        now: datetime,
        ttl: timedelta,
        authentication_method: str,
    ) -> str:
        expires_at = now + ttl
        return self._sign(
            {
                "kind": "action",
                "sub": str(user_id),
                "action": action,
                "object_id": str(object_id),
                "object_version": object_version,
                "exp": int(expires_at.timestamp()),
                "amr": authentication_method,
            }
        )

    def issue_review_reference(
        self,
        *,
        user_id: UUID,
        object_id: UUID,
        object_version: int,
        now: datetime,
        ttl: timedelta,
    ) -> str:
        return self._sign(
            {
                "kind": "review-reference",
                "sub": str(user_id),
                "object_id": str(object_id),
                "object_version": object_version,
                "exp": int((now + ttl).timestamp()),
            }
        )

    def issue_action_reference(
        self,
        *,
        user_id: UUID,
        action: str,
        object_id: UUID,
        object_version: int,
        now: datetime,
        ttl: timedelta,
    ) -> str:
        return self._sign(
            {
                "kind": "action-reference",
                "sub": str(user_id),
                "action": action,
                "object_id": str(object_id),
                "object_version": object_version,
                "exp": int((now + ttl).timestamp()),
            }
        )

    def verify_action_reference(
        self,
        token: str,
        *,
        user_id: UUID,
        action: str,
        object_id: UUID,
        object_version: int,
        now: datetime,
    ) -> ActionReference:
        payload = self._verify(token)
        expires_at = datetime.fromtimestamp(int(payload.get("exp", 0)), tz=UTC)
        expected = {
            "kind": "action-reference",
            "sub": str(user_id),
            "action": action,
            "object_id": str(object_id),
            "object_version": object_version,
        }
        if any(payload.get(key) != value for key, value in expected.items()):
            raise DomainRejected(
                "ACTION_REFERENCE_SCOPE_INVALID",
                "action reference does not match this user or campaign version",
            )
        if expires_at <= now:
            raise DomainRejected("ACTION_REFERENCE_EXPIRED", "action reference has expired")
        return ActionReference(
            user_id=user_id,
            action=action,
            object_id=object_id,
            object_version=object_version,
            expires_at=expires_at,
        )

    def verify_action_grant(
        self,
        token: str,
        *,
        user_id: UUID,
        action: str,
        object_id: UUID,
        object_version: int,
        now: datetime,
    ) -> ActionGrant:
        payload = self._verify(token)
        expires_at = datetime.fromtimestamp(int(payload.get("exp", 0)), tz=UTC)
        expected = {
            "kind": "action",
            "sub": str(user_id),
            "action": action,
            "object_id": str(object_id),
            "object_version": object_version,
        }
        if any(payload.get(key) != value for key, value in expected.items()):
            raise DomainRejected(
                "ACTION_GRANT_SCOPE_INVALID",
                "action authentication does not match this object version",
            )
        if expires_at <= now:
            raise DomainRejected("ACTION_GRANT_EXPIRED", "action authentication has expired")
        return ActionGrant(
            user_id=user_id,
            action=action,
            object_id=object_id,
            object_version=object_version,
            expires_at=expires_at,
            authentication_method=str(payload.get("amr", "unknown")),
        )
