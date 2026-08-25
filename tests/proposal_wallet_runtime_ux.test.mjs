import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import vm from "node:vm";

const webSource = name => fs.readFileSync(
  new URL(`../src/trading_control_plane/web/${name}`, import.meta.url),
  "utf8",
);

function capitalContext(providers) {
  const context = vm.createContext({
    console,
    window: {
      ethereum: {providers},
      addEventListener() {},
      dispatchEvent() {},
    },
    Event: class Event {
      constructor(type) { this.type = type; }
    },
    document: {querySelector: () => null, querySelectorAll: () => []},
    ARBITRUM_WALLET_CHAIN: {chainId:"0xa4b1"},
  });
  vm.runInContext(webSource("capital.js"), context);
  return context;
}

test("capital wallet selection matches the required address in every connected account", async () => {
  const required = "0x2222222222222222222222222222222222222222";
  const other = "0x1111111111111111111111111111111111111111";
  const unrelated = {
    request: async ({method}) => method === "eth_accounts" ? [other] : "0xa4b1",
  };
  const requiredProvider = {
    request: async ({method}) => {
      if (method === "eth_accounts" || method === "eth_requestAccounts") return [other, required];
      if (method === "eth_chainId") return "0xa4b1";
      throw new Error(`unexpected ${method}`);
    },
  };
  const context = capitalContext([unrelated, requiredProvider]);
  context.requiredAddress = required;
  const result = await vm.runInContext("connectedArbitrumWallet(requiredAddress)", context);
  assert.equal(result.provider, requiredProvider);
  assert.equal(result.account, required);
});

test("capital wallet selection still fails closed when no provider exposes the required address", async () => {
  const provider = account => ({
    request: async ({method}) => method === "eth_accounts" ? [account] : "0xa4b1",
  });
  const context = capitalContext([
    provider("0x1111111111111111111111111111111111111111"),
    provider("0x2222222222222222222222222222222222222222"),
  ]);
  context.requiredAddress = "0x3333333333333333333333333333333333333333";
  await assert.rejects(
    vm.runInContext("walletProvider(requiredAddress)", context),
    error => error?.code === "WALLET_PROVIDER_SELECTION_REQUIRED",
  );
});

test("manual proposal validation errors explain the exact safe correction", () => {
  const context = vm.createContext({
    console,
    currentLanguage:"zh-CN",
    session:null,
    location:{search:"", pathname:"/"},
    document:{querySelector:() => null, querySelectorAll:() => []},
    window:{},
  });
  vm.runInContext(webSource("shared.js"), context);
  for (const [code, expected] of [
    ["POSITION_NOTIONAL_TOO_SMALL", "最小下单金额"],
    ["INITIAL_POSITION_EXHAUSTS_CAP", "自动加仓"],
    ["PROPOSAL_RISK_EXCEEDED", "实际风险超过最大风险"],
  ]) {
    context.errorCode = code;
    const message = vm.runInContext("friendlyApiError({code:errorCode})", context);
    assert.match(message, new RegExp(expected));
    assert.doesNotMatch(message, /系统暂时无法完成请求/);
  }
});
