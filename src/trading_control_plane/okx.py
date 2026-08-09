from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from trading_control_plane.domain import DomainRejected

JsonFetcher = Callable[[str, dict[str, str], float], dict[str, Any]]


def _default_fetcher(url: str, headers: dict[str, str], timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=headers, method="GET")  # noqa: S310
    body: bytes | None = None
    last_error: BaseException | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                body = response.read()
            break
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403}:
                code = "OKX_AUTHENTICATION_FAILED"
            elif exc.code == 429:
                code = "OKX_RATE_LIMITED"
            else:
                code = "OKX_READ_ONLY_UNAVAILABLE"
            if exc.code >= 500 and attempt < 2:
                last_error = exc
                time.sleep(0.25 * 2**attempt)
                continue
            raise DomainRejected(code, "OKX read-only API rejected the request") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(0.25 * 2**attempt)
                continue
    if body is None:
        raise DomainRejected(
            "OKX_READ_ONLY_UNAVAILABLE", "OKX read-only API could not be reached"
        ) from last_error
    try:
        value = json.loads(body)
    except json.JSONDecodeError as exc:
        raise DomainRejected("OKX_RESPONSE_INVALID", "OKX returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise DomainRejected("OKX_RESPONSE_INVALID", "OKX response shape is invalid")
    return value


class OkxReadOnlyClient:
    """Narrow OKX private account reader with no write or trading methods."""

    def __init__(
        self,
        *,
        base_url: str = "https://www.okx.com",
        api_key: str,
        api_secret: str,
        passphrase: str,
        fetcher: JsonFetcher = _default_fetcher,
    ) -> None:
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme != "https" or parsed.hostname != "www.okx.com":
            raise ValueError("OKX read-only base URL must use the official API host")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._api_secret = api_secret
        self._passphrase = passphrase
        self._fetcher = fetcher

    @staticmethod
    def _timestamp(now: datetime) -> str:
        return now.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def verify_connection(self, *, now: datetime) -> None:
        path = "/api/v5/account/balance"
        timestamp = self._timestamp(now)
        signature = base64.b64encode(
            hmac.new(
                self._api_secret.encode(),
                f"{timestamp}GET{path}".encode(),
                hashlib.sha256,
            ).digest()
        ).decode()
        raw = self._fetcher(
            f"{self._base_url}{path}",
            {
                "OK-ACCESS-KEY": self._api_key,
                "OK-ACCESS-SIGN": signature,
                "OK-ACCESS-TIMESTAMP": timestamp,
                "OK-ACCESS-PASSPHRASE": self._passphrase,
            },
            5.0,
        )
        code = raw.get("code")
        if code != "0":
            normalized = str(code or "")
            if normalized in {"50011", "50040", "50061"}:
                error_code = "OKX_RATE_LIMITED"
            elif normalized.startswith("501"):
                error_code = "OKX_AUTHENTICATION_FAILED"
            else:
                error_code = "OKX_READ_ONLY_REJECTED"
            raise DomainRejected(error_code, "OKX rejected the read-only account probe")
        if not isinstance(raw.get("data"), list):
            raise DomainRejected("OKX_RESPONSE_INVALID", "OKX account response is invalid")
