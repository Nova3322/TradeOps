from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from trading_control_plane.domain import DomainRejected
from trading_control_plane.security_encoding import urlsafe_decode, urlsafe_encode

SCRYPT_N = 1 << 14
SCRYPT_R = 8
SCRYPT_P = 1
SALT_BYTES = 16
KEY_BYTES = 32
MAX_MEMORY = 64 * 1024 * 1024


class PasswordHasher:
    """Versioned scrypt password boundary; plaintext never leaves the request/setup process."""

    def __init__(self) -> None:
        self.dummy_hash = self.hash("dummy-password-used-only-to-equalize-login-work")

    def hash(self, password: str) -> str:
        self.validate(password)
        salt = secrets.token_bytes(SALT_BYTES)
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=SCRYPT_N,
            r=SCRYPT_R,
            p=SCRYPT_P,
            dklen=KEY_BYTES,
            maxmem=MAX_MEMORY,
        )
        return (
            f"scrypt$n={SCRYPT_N},r={SCRYPT_R},p={SCRYPT_P}"
            f"${urlsafe_encode(salt)}${urlsafe_encode(digest)}"
        )

    def verify(self, password: str, encoded: str) -> bool:
        try:
            algorithm, parameters, salt_text, digest_text = encoded.split("$", 3)
            values = dict(item.split("=", 1) for item in parameters.split(","))
            if algorithm != "scrypt":
                return False
            n, r, p = int(values["n"]), int(values["r"]), int(values["p"])
            if (n, r, p) != (SCRYPT_N, SCRYPT_R, SCRYPT_P):
                return False
            expected = urlsafe_decode(digest_text)
            actual = hashlib.scrypt(
                password.encode("utf-8"),
                salt=urlsafe_decode(salt_text),
                n=n,
                r=r,
                p=p,
                dklen=len(expected),
                maxmem=MAX_MEMORY,
            )
        except (KeyError, TypeError, ValueError):
            return False
        return hmac.compare_digest(actual, expected)

    @staticmethod
    def validate(password: str) -> None:
        if len(password) < 12 or len(password) > 128:
            raise DomainRejected(
                "PASSWORD_INVALID",
                "password must contain between 12 and 128 characters",
            )
        if password.isspace():
            raise DomainRejected("PASSWORD_INVALID", "password cannot contain only whitespace")


@dataclass
class LoginAttemptLimiter:
    max_attempts: int = 5
    window: timedelta = timedelta(minutes=15)
    lockout: timedelta = timedelta(minutes=15)
    _failures: dict[str, list[datetime]] = field(default_factory=dict)
    _locked_until: dict[str, datetime] = field(default_factory=dict)

    def retry_after(self, key: str, *, now: datetime) -> int | None:
        locked_until = self._locked_until.get(key)
        if locked_until is None or locked_until <= now:
            self._locked_until.pop(key, None)
            return None
        return max(1, int((locked_until - now).total_seconds()))

    def fail(self, key: str, *, now: datetime) -> int | None:
        threshold = now - self.window
        failures = [item for item in self._failures.get(key, []) if item > threshold]
        failures.append(now)
        self._failures[key] = failures
        if len(failures) < self.max_attempts:
            return None
        locked_until = now + self.lockout
        self._locked_until[key] = locked_until
        self._failures.pop(key, None)
        return int(self.lockout.total_seconds())

    def success(self, key: str) -> None:
        self._failures.pop(key, None)
        self._locked_until.pop(key, None)


@dataclass
class ApiClientRateLimiter:
    max_requests: int = 120
    window: timedelta = timedelta(minutes=1)
    _requests: dict[str, list[datetime]] = field(default_factory=dict)

    def consume(self, key: str, *, now: datetime) -> int | None:
        threshold = now - self.window
        requests = [item for item in self._requests.get(key, []) if item > threshold]
        if len(requests) >= self.max_requests:
            retry_at = min(requests) + self.window
            return max(1, int((retry_at - now).total_seconds()))
        requests.append(now)
        self._requests[key] = requests
        return None
