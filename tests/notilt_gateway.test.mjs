import assert from "node:assert/strict";
import test from "node:test";

import {
  createNoTiltClient,
  getProductionDeployment,
} from "@notilt/sdk";
import { zeroAddress } from "viem";

import { executeOperation } from "../src/trading_control_plane/notilt_gateway/index.mjs";

const deployment = getProductionDeployment(42161);
const vault = "0x1111111111111111111111111111111111111111";
const agent = "0x2222222222222222222222222222222222222222";
const owner = "0x3333333333333333333333333333333333333333";
const otherVault = "0x4444444444444444444444444444444444444444";
const otherAgent = "0x5555555555555555555555555555555555555555";
const requestId = `0x${"a".repeat(64)}`;

function publicClient(overrides = {}) {
  const state = {
    official: true,
    active: true,
    assigned: vault,
    owner,
    panic: false,
    maxReleaseNet: 100_000_000n,
    balance: 200_000_000n,
    allowance: 0n,
    blockTimestamp: 2_000n,
    request: [
      true,
      false,
      false,
      agent,
      deployment.assets[1].address,
      25_000_000n,
      1n,
      25_000_001n,
      1n,
      1_900n,
      2_100n,
      1n,
      1n,
    ],
    ...overrides,
  };
  return {
    chain: { id: deployment.chainId },
    async getBlock() {
      return { number: 123n, timestamp: state.blockTimestamp };
    },
    async getBalance() {
      return state.balance;
    },
    async readContract(input) {
      switch (input.functionName) {
        case "owner":
          return state.owner;
        case "registeredVault":
          return state.official;
        case "whitelistActive":
          return state.active;
        case "officialWhitelistVault":
          return state.assigned;
        case "maxReleaseNet":
          return state.maxReleaseNet;
        case "todayPendingNetOccupied":
          return 0n;
        case "isPanicLocked":
          return state.panic;
        case "dailyReleaseRate":
        case "dailyFeeRate":
          return 1n;
        case "balanceOf":
          return state.balance;
        case "allowance":
          return state.allowance;
        case "isSupportedAsset":
          return true;
        case "pendingWhitelistReleases":
          return state.request;
        default:
          throw new Error(`unexpected read ${input.functionName}`);
      }
    },
  };
}

function client(overrides = {}) {
  return createNoTiltClient({
    deployment,
    publicClient: publicClient(overrides),
  });
}

test("read-vault returns catalog assets and single-block budget metadata", async () => {
  const result = await executeOperation(
    { operation: "read-vault", chainId: 42161, vault, agent },
    { client: client() },
  );
  assert.equal(result.chain, "arbitrum");
  assert.equal(result.budgets.length, 3);
  assert.equal(result.budgets[1].isOfficialVault, true);
  assert.equal(result.budgets[1].isActiveWhitelist, true);
  assert.equal(result.budgets[1].maxReleaseNet, "100000000");
  assert.equal(result.budgets[1].blockNumber, "123");
});

test("agent release request rejects every invalid budget condition", async () => {
  const base = {
    operation: "prepare-release-request",
    chainId: 42161,
    vault,
    agent,
    asset: "USDC",
    amount: "25",
  };
  for (const [overrides, message] of [
    [{ official: false }, /not registered/],
    [{ active: false }, /not an active/],
    [{ assigned: otherVault }, /does not assign/],
    [{ owner: agent }, /owner operations/],
    [{ panic: true }, /panic locked/],
    [{ maxReleaseNet: 1n }, /exceeds current/],
  ]) {
    await assert.rejects(
      executeOperation(base, { client: client(overrides) }),
      message,
    );
  }
  await assert.rejects(
    executeOperation({ ...base, amount: "0" }, { client: client() }),
    /greater than zero/,
  );
  await assert.rejects(
    executeOperation({ ...base, amount: "-1" }, { client: client() }),
    /non-negative decimal string/,
  );
});

test("valid release request returns one unsigned constrained transaction", async () => {
  const result = await executeOperation(
    {
      operation: "prepare-release-request",
      chainId: 42161,
      vault,
      agent,
      asset: "USDC",
      amount: "25",
    },
    { client: client() },
  );
  assert.equal(result.transaction.chainId, 42161);
  assert.equal(result.transaction.to.toLowerCase(), vault.toLowerCase());
  assert.equal(result.transaction.functionName, "requestWhitelistRelease");
  assert.equal(result.transaction.value, "0");
  assert.match(result.transaction.data, /^0x[0-9a-f]+$/);
});

test("release execution rejects early, expired, and requester mismatch", async () => {
  const input = {
    operation: "prepare-release-execution",
    chainId: 42161,
    vault,
    agent,
    requestId,
  };
  await assert.rejects(
    executeOperation(input, {
      client: client({ blockTimestamp: 1_800n }),
    }),
    /unlocks at/,
  );
  await assert.rejects(
    executeOperation(input, {
      client: client({ blockTimestamp: 2_100n }),
    }),
    /expired at/,
  );
  const mismatch = publicClient();
  const originalRead = mismatch.readContract;
  mismatch.readContract = async (request) => {
    if (request.functionName === "pendingWhitelistReleases") {
      const value = await originalRead(request);
      return value.map((item, index) => (index === 3 ? otherAgent : item));
    }
    return originalRead(request);
  };
  await assert.rejects(
    executeOperation(input, {
      client: createNoTiltClient({ deployment, publicClient: mismatch }),
    }),
    /not the requester/,
  );
});

test("release execution and cancellation remain fixed-purpose unsigned calls", async () => {
  const execution = await executeOperation(
    {
      operation: "prepare-release-execution",
      chainId: 42161,
      vault,
      agent,
      requestId,
    },
    { client: client() },
  );
  const cancellation = await executeOperation(
    {
      operation: "prepare-release-cancellation",
      chainId: 42161,
      vault,
      agent,
      requestId,
    },
    { client: client() },
  );
  assert.equal(execution.functionName, "executeWhitelistRelease");
  assert.equal(cancellation.functionName, "cancelWhitelistRelease");
});

test("ERC20 deposit uses exact allowance sequence", async () => {
  const input = {
    operation: "prepare-deposit",
    chainId: 42161,
    vault,
    agent,
    asset: "USDC",
    amount: "25",
  };
  const zero = await executeOperation(input, {
    client: client({ allowance: 0n }),
  });
  assert.deepEqual(
    zero.transactions.map((item) => item.functionName),
    ["approve", "deposit"],
  );

  const enough = await executeOperation(input, {
    client: client({ allowance: 25_000_000n }),
  });
  assert.deepEqual(
    enough.transactions.map((item) => item.functionName),
    ["deposit"],
  );

  const reset = await executeOperation(input, {
    client: client({ allowance: 1n }),
  });
  assert.deepEqual(
    reset.transactions.map((item) => item.functionName),
    ["approve", "approve", "deposit"],
  );
});

test("native deposit sends the exact amount as transaction value", async () => {
  const result = await executeOperation(
    {
      operation: "prepare-deposit",
      chainId: 42161,
      vault,
      agent,
      asset: "ETH",
      amount: "0.01",
    },
    { client: client() },
  );
  assert.equal(result.transactions.length, 1);
  assert.equal(result.transactions[0].functionName, "deposit");
  assert.equal(result.transactions[0].value, "10000000000000000");
});

test("untrusted chains, assets, addresses, and operations are rejected", async () => {
  await assert.rejects(
    executeOperation({
      operation: "read-vault",
      chainId: 10,
      vault,
      agent,
    }),
    /Ethereum, BNB Smart Chain, or Arbitrum/,
  );
  await assert.rejects(
    executeOperation(
      {
        operation: "prepare-deposit",
        chainId: 42161,
        vault,
        agent,
        asset: "DAI",
        amount: "1",
      },
      { client: client() },
    ),
    /official deployment catalog/,
  );
  await assert.rejects(
    executeOperation({
      operation: "read-vault",
      chainId: 42161,
      vault: "invalid",
      agent,
    }),
    /valid EVM address/,
  );
  await assert.rejects(
    executeOperation({
      operation: "requestWhitelistAdd",
      chainId: 42161,
      vault,
      agent,
    }),
    /operation is not supported/,
  );
});

test("assignment lookup only reads the official registry binding", async () => {
  const active = await executeOperation(
    { operation: "resolve-assignment", chainId: 42161, agent },
    { client: client() },
  );
  assert.equal(active.assignedVault.toLowerCase(), vault.toLowerCase());
  assert.equal(active.active, true);

  const unassigned = await executeOperation(
    { operation: "resolve-assignment", chainId: 42161, agent },
    { client: client({ assigned: zeroAddress }) },
  );
  assert.equal(unassigned.active, false);
});
