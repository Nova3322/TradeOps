from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import datetime
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
                code = "BYBIT_AUTHENTICATION_FAILED"
            elif exc.code == 429:
                code = "BYBIT_RATE_LIMITED"
            else:
                code = "BYBIT_READ_ONLY_UNAVAILABLE"
            if exc.code >= 500 and attempt < 2:
                last_error = exc
                time.sleep(0.25 * 2**attempt)
                continue
            raise DomainRejected(code, "Bybit read-only API rejected the request") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(0.25 * 2**attempt)
                continue
    if body is None:
        raise DomainRejected(
            "BYBIT_READ_ONLY_UNAVAILABLE", "Bybit read-only API could not be reached"
        ) from last_error
    try:
        value = json.loads(body)
    except json.JSONDecodeError as exc:
        raise DomainRejected("BYBIT_RESPONSE_INVALID", "Bybit returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise DomainRejected("BYBIT_RESPONSE_INVALID", "Bybit response shape is invalid")
    return value


class BybitReadOnlyClient:
    """Narrow Bybit V5 unified-account reader with no write or trading methods."""

    def __init__(
        self,
        *,
        base_url: str = "https://api.bybit.com",
        api_key: str,
        api_secret: str,
        recv_window_ms: int = 5_000,
        fetcher: JsonFetcher = _default_fetcher,
    ) -> None:
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme != "https" or parsed.hostname != "api.bybit.com":
            raise ValueError("Bybit read-only base URL must use the official API host")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._api_secret = api_secret
        self._recv_window_ms = recv_window_ms
        self._fetcher = fetcher

    def verify_connection(self, *, now: datetime) -> None:
        path = "/v5/account/wallet-balance"
        query = "accountType=UNIFIED"
        timestamp = str(int(now.timestamp() * 1000))
        recv_window = str(self._recv_window_ms)
        signature = hmac.new(
            self._api_secret.encode(),
            f"{timestamp}{self._api_key}{recv_window}{query}".encode(),
            hashlib.sha256,
        ).hexdigest()
        raw = self._fetcher(
            f"{self._base_url}{path}?{query}",
            {
                "X-BAPI-API-KEY": self._api_key,
                "X-BAPI-TIMESTAMP": timestamp,
                "X-BAPI-RECV-WINDOW": recv_window,
                "X-BAPI-SIGN": signature,
            },
            5.0,
        )
        code = raw.get("retCode")
        if code != 0:
            if code == 10006:
                error_code = "BYBIT_RATE_LIMITED"
            elif code in {10003, 10004, 10005, 10007, 10010}:
                error_code = "BYBIT_AUTHENTICATION_FAILED"
            else:
                error_code = "BYBIT_READ_ONLY_REJECTED"
            raise DomainRejected(error_code, "Bybit rejected the read-only account probe")
        result = raw.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("list"), list):
            raise DomainRejected(
                "BYBIT_RESPONSE_INVALID", "Bybit unified account response is invalid"
            )
