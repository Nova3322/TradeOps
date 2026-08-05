from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from trading_control_plane.domain import DomainRejected

JsonObject = dict[str, Any]
GatewayExecutor = Callable[[JsonObject], JsonObject]


class SafeSpendingGateway:
    """Read-only Safe Allowance Module boundary; never signs or broadcasts."""

    def __init__(self, *, executor: GatewayExecutor | None = None, timeout_seconds: float = 20):
        self._executor = executor or self._execute_subprocess
        self._timeout_seconds = timeout_seconds
        self._gateway_path = Path(__file__).with_name("safe_spending_gateway") / "index.mjs"

    @property
    def available(self) -> bool:
        return self._gateway_path.is_file() and shutil.which("node") is not None

    def _execute_subprocess(self, payload: JsonObject) -> JsonObject:
        node = shutil.which("node")
        if node is None or not self._gateway_path.is_file():
            raise DomainRejected("SAFE_GATEWAY_UNAVAILABLE", "Safe gateway runtime is unavailable")
        try:
            completed = subprocess.run(  # noqa: S603
                [node, str(self._gateway_path)],
                input=json.dumps(payload, separators=(",", ":")),
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
                check=False,
                env={"PATH": os.environ.get("PATH", "")},
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DomainRejected("SAFE_GATEWAY_UNAVAILABLE", "Safe preflight timed out") from exc
        try:
            response = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise DomainRejected(
                "SAFE_RESPONSE_INVALID", "Safe gateway returned invalid JSON"
            ) from exc
        if completed.returncode != 0 or response.get("ok") is not True:
            message = str((response.get("error") or {}).get("message") or "Safe preflight failed")
            raise DomainRejected("SAFE_PREFLIGHT_REJECTED", message[:400])
        data = response.get("data")
        if not isinstance(data, dict):
            raise DomainRejected("SAFE_RESPONSE_INVALID", "Safe gateway omitted its result")
        return data

    def read_limit(self, *, rpc_url: str, safe: str, delegate: str) -> JsonObject:
        return self._executor(
            {
                "operation": "read-limit",
                "chainId": 42161,
                "rpcUrl": rpc_url,
                "safe": safe,
                "delegate": delegate,
                "asset": "USDC",
            }
        )

    def prepare_spend(
        self, *, rpc_url: str, safe: str, delegate: str, recipient: str, amount: str
    ) -> JsonObject:
        value = self._executor(
            {
                "operation": "prepare-spend",
                "chainId": 42161,
                "rpcUrl": rpc_url,
                "safe": safe,
                "delegate": delegate,
                "recipient": recipient,
                "asset": "USDC",
                "amount": amount,
            }
        )
        required = {
            "kind",
            "chainId",
            "module",
            "safe",
            "delegate",
            "token",
            "recipient",
            "amount",
            "nonce",
            "transferHash",
        }
        if not required.issubset(value) or value.get("kind") != "SAFE_ALLOWANCE_SIGNATURE_REQUEST":
            raise DomainRejected(
                "SAFE_RESPONSE_INVALID", "Safe gateway returned an invalid signature request"
            )
        return value

    def prepare_deposit(self, *, rpc_url: str, safe: str, sender: str, amount: str) -> JsonObject:
        value = self._executor(
            {
                "operation": "prepare-deposit",
                "chainId": 42161,
                "rpcUrl": rpc_url,
                "safe": safe,
                "sender": sender,
                "asset": "USDC",
                "amount": amount,
            }
        )
        required = {"kind", "chainId", "safe", "sender", "token", "amount", "to", "data"}
        if (
            not required.issubset(value)
            or value.get("kind") != "SAFE_ERC20_DEPOSIT_UNSIGNED_TRANSACTION"
            or value.get("signing") is not False
            or value.get("broadcast") is not False
        ):
            raise DomainRejected(
                "SAFE_RESPONSE_INVALID", "Safe gateway returned an invalid deposit transaction"
            )
        return value
