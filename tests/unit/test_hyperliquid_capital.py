from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from trading_control_plane import hyperliquid_capital
from trading_control_plane.domain import DomainRejected
from trading_control_plane.hyperliquid_capital import (
    ARBITRUM_NATIVE_USDC_ADDRESS,
    ERC20_TRANSFER_TOPIC,
    HYPERLIQUID_BRIDGE2_ADDRESS,
    HyperliquidCapitalGateway,
)

MAIN = "0x1111111111111111111111111111111111111111"
AGENT = "0x2222222222222222222222222222222222222222"
HASH = "0x" + "ab" * 32
NOW = datetime(2026, 8, 8, 8, 0, tzinfo=UTC)


def test_withdraw3_checks_agent_scope_and_falls_back_to_unsigned_human_wallet() -> None:
    requests: list[dict[str, object]] = []

    def fetcher(_url: str, payload: dict[str, object], _timeout: float) -> object:
        requests.append(payload)
        if payload["type"] == "userAbstraction":
            return "default"
        if payload["type"] == "clearinghouseState":
            return {"withdrawable": "101"}
        assert payload == {"type": "userRole", "user": AGENT}
        return {"role": "agent", "data": {"user": MAIN}}

    artifact = HyperliquidCapitalGateway(info_fetcher=fetcher).prepare_withdrawal(
        base_url="https://api.hyperliquid.xyz",
        main_account=MAIN,
        api_wallet_address=AGENT,
        destination=MAIN,
        amount="100",
        max_fee="1",
        now=NOW,
    )

    assert [request["type"] for request in requests] == [
        "userAbstraction",
        "clearinghouseState",
        "userRole",
    ]
    assert artifact["kind"] == "HYPERLIQUID_WITHDRAW3_TYPED_REQUEST"
    assert artifact["agentWallet"]["authorized"] is True
    assert artifact["agentWallet"]["capability"] == ("TRADING_AGENT_ONLY_MANUAL_WALLET_FALLBACK")
    assert artifact["fallbackReason"] == "WITHDRAW3_REQUIRES_USER_SIGNED_ACTION"
    assert artifact["typedData"]["primaryType"] == "HyperliquidTransaction:Withdraw"
    assert artifact["exchangeRequestTemplate"]["signature"] is None
    assert artifact["signing"] is False
    assert artifact["broadcast"] is False


def test_bridge_deposit_is_fixed_to_native_usdc_and_official_bridge() -> None:
    def fetcher(_url: str, _payload: dict[str, object], _timeout: float) -> object:
        return {"role": "agent", "data": {"user": MAIN}}

    artifact = HyperliquidCapitalGateway(info_fetcher=fetcher).prepare_deposit(
        base_url="https://api.hyperliquid.xyz",
        main_account=MAIN,
        api_wallet_address=AGENT,
        owned_arbitrum_address=MAIN,
        bridge_address=HYPERLIQUID_BRIDGE2_ADDRESS,
        amount="5.25",
        now=NOW,
    )

    assert artifact["chainId"] == 42161
    assert artifact["to"] == ARBITRUM_NATIVE_USDC_ADDRESS
    assert artifact["bridge"] == HYPERLIQUID_BRIDGE2_ADDRESS
    assert artifact["method"] == "ERC20.transfer"
    assert artifact["amountRaw"] == "5250000"
    assert artifact["data"].startswith("0xa9059cbb")
    assert artifact["signing"] is False and artifact["broadcast"] is False

    with pytest.raises(DomainRejected) as mismatched_bridge:
        HyperliquidCapitalGateway(info_fetcher=fetcher).prepare_deposit(
            base_url="https://api.hyperliquid.xyz",
            main_account=MAIN,
            api_wallet_address=AGENT,
            owned_arbitrum_address=MAIN,
            bridge_address="0x3333333333333333333333333333333333333333",
            amount="5.25",
            now=NOW,
        )
    assert mismatched_bridge.value.code == "HYPERLIQUID_BRIDGE_UNTRUSTED"


def test_arbitrum_usdc_balance_reads_exact_native_usdc_balance() -> None:
    def rpc_fetcher(
        _url: str, method: str, params: list[object], _timeout: float
    ) -> object:
        assert method == "eth_call"
        call = params[0]
        assert isinstance(call, dict)
        assert call["to"] == ARBITRUM_NATIVE_USDC_ADDRESS
        assert str(call["data"]).startswith("0x70a08231")
        assert params[1] == "latest"
        return hex(5_100_000)

    balance = HyperliquidCapitalGateway(
        rpc_fetcher=rpc_fetcher
    ).arbitrum_usdc_balance(
        rpc_url="https://arb.example.invalid/rpc",
        address=MAIN,
    )

    assert balance == Decimal("5.1")

def test_exact_arbitrum_usdc_transfer_is_ready_for_browser_wallet() -> None:
    artifact = HyperliquidCapitalGateway().prepare_arbitrum_usdc_transfer(
        sender=MAIN,
        destination=AGENT,
        amount="12.5",
        now=NOW,
    )

    assert artifact["kind"] == "ARBITRUM_USDC_UNSIGNED_TRANSACTION"
    assert artifact["from"] == MAIN
    assert artifact["to"] == ARBITRUM_NATIVE_USDC_ADDRESS
    assert artifact["recipient"] == AGENT
    assert artifact["amountRaw"] == "12500000"
    assert artifact["data"].startswith("0xa9059cbb")
    assert artifact["data"][34:74] == AGENT[2:]
    assert artifact["signing"] is False and artifact["broadcast"] is False


def test_withdrawable_shortfall_does_not_build_internal_class_transfer() -> None:
    def fetcher(_url: str, payload: dict[str, object], _timeout: float) -> object:
        if payload["type"] == "userAbstraction":
            return "default"
        if payload["type"] == "clearinghouseState":
            return {"withdrawable": "20"}
        if payload["type"] == "spotClearinghouseState":
            return {"balances": [{"coin": "USDC", "total": "90", "hold": "5"}]}
        assert payload["type"] == "userRole"
        return {"role": "agent", "data": {"user": MAIN}}

    with pytest.raises(DomainRejected) as caught:
        HyperliquidCapitalGateway(info_fetcher=fetcher).prepare_withdrawal(
            base_url="https://api.hyperliquid.xyz",
            main_account=MAIN,
            api_wallet_address=AGENT,
            destination=MAIN,
            amount="100",
            max_fee="1",
            now=NOW,
        )

    assert caught.value.code == "HYPERLIQUID_WITHDRAWABLE_INSUFFICIENT"


def test_unified_account_withdrawal_uses_available_spot_usdc() -> None:
    requests: list[str] = []

    def fetcher(_url: str, payload: dict[str, object], _timeout: float) -> object:
        request_type = str(payload["type"])
        requests.append(request_type)
        if request_type == "userAbstraction":
            return "unifiedAccount"
        if request_type == "spotClearinghouseState":
            return {
                "balances": [
                    {"coin": "USDC", "total": "10.01", "hold": "0.01"},
                    {"coin": "HYPE", "total": "2", "hold": "0"},
                ]
            }
        assert request_type == "userRole"
        return {"role": "agent", "data": {"user": MAIN}}

    artifact = HyperliquidCapitalGateway(info_fetcher=fetcher).prepare_withdrawal(
        base_url="https://api.hyperliquid.xyz",
        main_account=MAIN,
        api_wallet_address=AGENT,
        destination=MAIN,
        amount="8",
        max_fee="1",
        now=NOW,
    )

    assert requests == ["userAbstraction", "spotClearinghouseState", "userRole"]
    assert artifact["withdrawableObserved"] == "10.00"
    assert artifact["withdrawableSource"] == "UNIFIED_SPOT_USDC"
    assert artifact["accountAbstraction"] == "unifiedAccount"


def test_deposit_rejects_account_mismatch_minimum_and_excess_precision() -> None:
    gateway = HyperliquidCapitalGateway(
        info_fetcher=lambda *_args: {"role": "agent", "data": {"user": MAIN}}
    )
    common = {
        "base_url": "https://api.hyperliquid.xyz",
        "main_account": MAIN,
        "api_wallet_address": AGENT,
        "bridge_address": HYPERLIQUID_BRIDGE2_ADDRESS,
        "now": NOW,
    }
    with pytest.raises(DomainRejected) as mismatch:
        gateway.prepare_deposit(
            **common,
            owned_arbitrum_address="0x3333333333333333333333333333333333333333",
            amount="5",
        )
    assert mismatch.value.code == "HYPERLIQUID_DEPOSIT_ACCOUNT_MISMATCH"
    with pytest.raises(DomainRejected) as minimum:
        gateway.prepare_deposit(**common, owned_arbitrum_address=MAIN, amount="4.999999")
    assert minimum.value.code == "HYPERLIQUID_DEPOSIT_BELOW_MINIMUM"
    with pytest.raises(DomainRejected) as precision:
        gateway.prepare_deposit(**common, owned_arbitrum_address=MAIN, amount="5.0000001")
    assert precision.value.code == "HYPERLIQUID_CAPITAL_AMOUNT_PRECISION_INVALID"


def test_receipts_require_exact_public_ledger_and_arbitrum_evidence() -> None:
    def info_fetcher(_url: str, payload: dict[str, object], _timeout: float) -> object:
        assert payload["type"] == "userNonFundingLedgerUpdates"
        return [
            {
                "time": int(NOW.timestamp() * 1000),
                "hash": HASH,
                "delta": {"type": "withdraw", "usdc": "9", "nonce": 42_000, "fee": "1"},
            }
        ]

    expected_data = (
        "0xa9059cbb"
        + HYPERLIQUID_BRIDGE2_ADDRESS[2:].rjust(64, "0")
        + hex(9_000_000)[2:].rjust(64, "0")
    )

    def rpc_fetcher(_url: str, method: str, _params: list[object], _timeout: float) -> object:
        if method == "eth_getTransactionReceipt":
            return {"status": "0x1", "blockNumber": "0x64", "logs": []}
        if method == "eth_getTransactionByHash":
            return {
                "from": MAIN,
                "to": ARBITRUM_NATIVE_USDC_ADDRESS,
                "input": expected_data,
            }
        assert method == "eth_blockNumber"
        return "0x78"

    gateway = HyperliquidCapitalGateway(info_fetcher=info_fetcher, rpc_fetcher=rpc_fetcher)
    ledger = gateway.verify_hyperliquid_ledger(
        base_url="https://api.hyperliquid.xyz",
        main_account=MAIN,
        receipt_kind="WITHDRAWAL",
        amount="9",
        prepared_at=NOW,
        nonce=42,
        action_hash=HASH,
        now=NOW,
    )
    assert ledger["kind"] == "HYPERLIQUID_WITHDRAW_LEDGER_RECEIPT"
    assert ledger["nonce"] == 42_000
    assert ledger["signedNonce"] == 42
    transfer = gateway.verify_arbitrum_usdc_transfer(
        rpc_url="https://rpc.example.invalid",
        transaction_hash=HASH,
        sender=MAIN,
        recipient=HYPERLIQUID_BRIDGE2_ADDRESS,
        amount="9",
        min_confirmations=20,
    )
    assert transfer["confirmations"] == 21


def test_withdrawal_credit_matches_bridge_transfer_log() -> None:
    recipient_topic = f"0x{MAIN[2:].rjust(64, '0')}"
    bridge_topic = f"0x{HYPERLIQUID_BRIDGE2_ADDRESS[2:].rjust(64, '0')}"

    def rpc_fetcher(_url: str, method: str, _params: list[object], _timeout: float) -> object:
        if method == "eth_getTransactionReceipt":
            return {
                "status": "0x1",
                "blockNumber": "0x64",
                "logs": [
                    {
                        "address": ARBITRUM_NATIVE_USDC_ADDRESS,
                        "topics": [ERC20_TRANSFER_TOPIC, bridge_topic, recipient_topic],
                        "data": hex(int(Decimal("9") * Decimal(1_000_000))),
                    }
                ],
            }
        assert method == "eth_blockNumber"
        return "0x78"

    receipt = HyperliquidCapitalGateway(rpc_fetcher=rpc_fetcher).verify_arbitrum_usdc_credit(
        rpc_url="https://rpc.example.invalid",
        transaction_hash=HASH,
        sender=HYPERLIQUID_BRIDGE2_ADDRESS,
        recipient=MAIN,
        amount="9",
        min_confirmations=20,
    )
    assert receipt["kind"] == "ARBITRUM_USDC_CREDIT_RECEIPT"


def test_withdrawal_credit_can_be_discovered_without_exchange_evm_hash() -> None:
    recipient_topic = f"0x{MAIN[2:].rjust(64, '0')}"
    bridge_topic = f"0x{HYPERLIQUID_BRIDGE2_ADDRESS[2:].rjust(64, '0')}"
    calls: list[tuple[str, list[object]]] = []

    def rpc_fetcher(_url: str, method: str, params: list[object], _timeout: float) -> object:
        calls.append((method, params))
        if method == "eth_blockNumber":
            return "0x78"
        if method == "eth_getBlockByNumber":
            return {"timestamp": hex(int(NOW.timestamp()) + 60)}
        if method == "eth_getLogs":
            return [
                {
                    "address": ARBITRUM_NATIVE_USDC_ADDRESS,
                    "topics": [ERC20_TRANSFER_TOPIC, bridge_topic, recipient_topic],
                    "data": hex(9_000_000),
                    "blockNumber": "0x64",
                    "transactionHash": HASH,
                }
            ]
        assert method == "eth_getTransactionReceipt"
        return {
            "status": "0x1",
            "blockNumber": "0x64",
            "logs": [
                {
                    "address": ARBITRUM_NATIVE_USDC_ADDRESS,
                    "topics": [ERC20_TRANSFER_TOPIC, bridge_topic, recipient_topic],
                    "data": hex(9_000_000),
                }
            ],
        }

    receipt = HyperliquidCapitalGateway(rpc_fetcher=rpc_fetcher).find_arbitrum_usdc_credit(
        rpc_url="https://rpc.example.invalid",
        sender=HYPERLIQUID_BRIDGE2_ADDRESS,
        recipient=MAIN,
        amount="9",
        prepared_at=NOW,
        min_confirmations=20,
    )

    assert receipt["transactionHash"] == HASH
    log_filter = next(params[0] for method, params in calls if method == "eth_getLogs")
    assert log_filter["topics"] == [ERC20_TRANSFER_TOPIC, bridge_topic, recipient_topic]


def test_default_rpc_transport_uses_bounded_requests_client(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, object], float]] = []

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"jsonrpc": "2.0", "id": 1, "result": "0x78"}

    def post(url: str, *, json: dict[str, object], timeout: float) -> Response:
        calls.append((url, json, timeout))
        return Response()

    monkeypatch.setattr(hyperliquid_capital.requests, "post", post)
    gateway = HyperliquidCapitalGateway(timeout_seconds=4)

    assert gateway._rpc_fetcher(
        "https://arb1.arbitrum.io/rpc", "eth_blockNumber", [], 4
    ) == "0x78"
    assert calls == [
        (
            "https://arb1.arbitrum.io/rpc",
            {"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []},
            4,
        )
    ]
