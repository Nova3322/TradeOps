import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

const source = fs.readFileSync(
  new URL("../src/trading_control_plane/web/capital.js", import.meta.url),
  "utf8",
);

test("capital operations open the connected wallet and record public evidence automatically", () => {
  for (const marker of [
    "eth_requestAccounts",
    "wallet_switchEthereumChain",
    "eth_sendTransaction",
    "eth_signTypedData_v4",
    "recordDirectWalletOutcome",
  ]) {
    assert.equal(source.includes(marker), true, marker);
  }
  assert.equal(source.includes("`/api/capital?environment=${displayEnvironment}`, {\n    timeoutMs:45_000"), true);
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
