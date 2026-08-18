import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

const source = fs.readFileSync(
  new URL("../src/trading_control_plane/web/capital.js", import.meta.url),
  "utf8",
);

test("capital operations open the connected wallet and record public evidence automatically", () => {
  for (const marker of [
    "eip6963:requestProvider",
    "eip6963:announceProvider",
    "HUMAN_WALLET_CONFIRMATION_CANCELLED",
    "WALLET_CONFIRMATION_CANCELLED",
    "eth_requestAccounts",
    "wallet_switchEthereumChain",
    "eth_sendTransaction",
    "eth_signTypedData_v4",
    "recordDirectWalletOutcome",
    "hyperliquidSubmissionReason",
    "资金记录未标记为已提交",
  ]) {
    assert.equal(source.includes(marker), true, marker);
  }
  assert.equal(source.includes("`/api/capital?environment=${displayEnvironment}`, {\n    timeoutMs:45_000"), true);
  assert.equal(source.includes("WALLET_PROVIDER_NOT_AVAILABLE"), true);
  assert.equal(source.includes("timeoutMs:45_000"), true);
});

test("operation history has no second confirmation or manual hash forms", () => {
  for (const marker of [
    "binance-submit-form",
    "wallet-result-form",
    "记录钱包已提交",
    "再次核对币种、网络、白名单地址",
    "确认资金路径并继续？",
    'name="final_confirmed"',
  ]) {
    assert.equal(source.includes(marker), false, marker);
  }
  assert.equal(source.includes("prepareAndSubmitBinanceWithdrawal"), true);
  assert.equal(source.includes("币安转出通过受限 API 直接提交"), true);
  assert.equal(source.includes("capital-operation-actions"), false);
  assert.equal(source.includes("capital-transfer-continuations"), false);
  assert.equal(source.includes("待继续划转"), false);
  assert.equal(source.includes("在划转区完成下一步"), false);
});

test("NoTilt protocol delay keeps one-click wallet continuation without manual evidence", () => {
  for (const marker of [
    "prepareNoTiltReleaseExecution",
    "prepareNoTiltDestinationTransfer",
    "notilt-release-receipt",
    "协议解锁后执行并打开钱包",
  ]) {
    assert.equal(source.includes(marker), true, marker);
  }
});

test("Safe outbound flow verifies source receipt and completes the Hyperliquid second leg", () => {
  for (const marker of [
    "verifyTreasuryWithdrawalReceipt",
    "continueSafeOutboundExecution",
    "reconcilePendingSafeHyperliquidDeposits",
    "verifyHyperliquidDepositReceipts",
    "TREASURY_WITHDRAWAL_RECEIPT_CONFIRMED",
    "第一笔钱包交易已提交，正在等待授权地址到账",
    "授权地址已到账，正在自动准备 Hyperliquid 入金钱包确认",
    "已提交，正在自动续接入金",
    "{attempts:200, delayMs:3000}",
  ]) {
    assert.equal(source.includes(marker), true, marker);
  }
  assert.equal(source.includes("继续已提取资金并充值"), false);
  assert.ok(source.indexOf("verifyTreasuryWithdrawalReceipt") < source.indexOf("prepareHyperliquidWalletAction", source.indexOf("async function continueSafeOutboundExecution")));
});

test("exchange receipts and Binance wallet-class transfers complete automatically", () => {
  for (const marker of [
    "verifyHyperliquidWithdrawalReceipts",
    "verifyBinanceReceipt",
    "BINANCE_INTERNAL_TRANSFER_PENDING",
    "BINANCE_INTERNAL_TRANSFER_PERMISSION_DISABLED",
    "TRANSFER_BINANCE_USDM_TO_SPOT",
    "TRANSFER_BINANCE_SPOT_TO_USDM",
    "链上确认中",
    "链上已确认",
  ]) {
    assert.equal(source.includes(marker), true, marker);
  }
});

test("latest capital operation stays visible and pending Hyperliquid receipts resume automatically", () => {
  for (const marker of [
    "renderDirectCapitalLiveProgress",
    "directCapitalCurrentPhase",
    "reconcilePendingHyperliquidWithdrawals",
    "钱包提交成功，正在核对 Hyperliquid 账本",
    "Hyperliquid 提现已确认，正在等待 Arbitrum / Safe 到账",
    "Safe 已到账，公开回执已确认",
    "查看 Arbitrum 回执",
    "reconcilePendingHyperliquidDeposits",
    "Hyperliquid 入金已提交，回执核对暂缓",
    "系统不会重复发送",
  ]) {
    assert.equal(source.includes(marker), true, marker);
  }
});

test("operation receipts open by default and paginate at bounded 50 or 100 rows", () => {
  for (const marker of [
    'class="capital-activity-disclosure" open',
    "capitalOperationsPageSize = 50",
    'value="50"',
    'value="100"',
    "data-capital-operation-page-size",
    "data-capital-operation-page",
    "directOperations.slice(",
  ]) {
    assert.equal(source.includes(marker), true, marker);
  }
  assert.equal(source.includes('value="200"'), false);
});

test("wallet and blocker details use one structured assurance panel", () => {
  for (const marker of [
    "renderCapitalOperationAssurance",
    "renderCapitalHandoffCard",
    "capital-operation-details",
    "capital-handoff-grid",
    "校验 / 签名",
    "查看校验与签名摘要",
  ]) {
    assert.equal(source.includes(marker), true, marker);
  }
  assert.equal(source.includes('<details class="capital-wallet-handoff">'), false);
});
