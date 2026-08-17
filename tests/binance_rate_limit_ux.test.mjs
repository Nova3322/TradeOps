import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

const shared = fs.readFileSync(
  new URL("../src/trading_control_plane/web/shared.js", import.meta.url),
  "utf8",
);
const accounts = fs.readFileSync(
  new URL("../src/trading_control_plane/web/accounts.js", import.meta.url),
  "utf8",
);
const apiClient = fs.readFileSync(
  new URL("../src/trading_control_plane/web/api-client.js", import.meta.url),
  "utf8",
);

test("Binance connection diagnostics distinguish rate limit causes and retry time", () => {
  for (const marker of [
    "ORDINARY_RATE_LIMIT:'普通请求限流'",
    "REQUEST_WEIGHT_EXCEEDED:'请求权重超限'",
    "IP_TEMPORARILY_BANNED:'IP 临时封禁'",
    "fmtBinanceConnectionDiagnostic",
  ]) {
    assert.equal(shared.includes(marker), true, marker);
  }
  for (const marker of [
    "HTTP Status",
    "Binance error code",
    "Binance message",
    "Retry-After",
    "限流 / weight headers",
    "到期前不会重复请求 Binance",
  ]) {
    assert.equal(accounts.includes(marker), true, marker);
  }
  assert.equal(apiClient.includes("error.details = data?.error?.details"), true);
});
