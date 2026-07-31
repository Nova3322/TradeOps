// Signing-free NoTilt SDK boundary used by the Python control plane.
import {
  createNoTiltClient,
  getProductionDeployment,
  parseAssetAmount,
  serializeUnsignedTransaction,
} from "@notilt/sdk";
import { getAddress, isAddress, zeroAddress } from "viem";
import { pathToFileURL } from "node:url";

const SUPPORTED_CHAIN_IDS = new Set([1, 56, 42161]);
const SUPPORTED_OPERATIONS = new Set([
  "resolve-assignment",
  "verify-deployment",
  "read-vault",
  "prepare-deposit",
  "prepare-release-request",
  "prepare-release-execution",
  "prepare-release-cancellation",
]);

function requireObject(value) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("Gateway input must be a JSON object.");
  }
  return value;
}

function requireChainId(value) {
  const chainId = Number(value);
  if (!Number.isInteger(chainId) || !SUPPORTED_CHAIN_IDS.has(chainId)) {
    throw new Error("NoTilt chain must be Ethereum, BNB Smart Chain, or Arbitrum One.");
  }
  return chainId;
}

function requireAddress(value, field) {
  if (typeof value !== "string" || !isAddress(value)) {
    throw new Error(`${field} must be a valid EVM address.`);
  }
  return getAddress(value);
}

function requireRequestId(value) {
  if (typeof value !== "string" || !/^0x[0-9a-fA-F]{64}$/.test(value)) {
    throw new Error("requestId must be a bytes32 value.");
  }
  return value;
}

function requireAsset(deployment, value) {
  if (typeof value !== "string" || !/^[A-Za-z0-9]+$/.test(value)) {
    throw new Error("asset must be a catalog symbol.");
  }
  const asset = deployment.assets.find(
    (candidate) => candidate.symbol.toLowerCase() === value.toLowerCase(),
  );
  if (!asset) {
    throw new Error("asset is not in the official deployment catalog.");
  }
  return asset;
}

function requireAmount(value, decimals) {
  if (typeof value !== "string") {
    throw new Error("amount must be a decimal string.");
  }
  const amount = parseAssetAmount(value, decimals);
  if (amount <= 0n) {
    throw new Error("amount must be greater than zero.");
  }
  return amount;
}

function serializeBudget(budget, asset) {
  return {
    blockNumber: budget.blockNumber.toString(),
    blockTimestamp: budget.blockTimestamp.toString(),
    vault: budget.vault,
    agent: budget.agent,
    asset: {
      address: asset.address,
      symbol: asset.symbol,
      decimals: asset.decimals,
      native: asset.native,
    },
    owner: budget.owner,
    isOfficialVault: budget.isOfficialVault,
    isActiveWhitelist: budget.isActiveWhitelist,
    assignedWhitelistVault: budget.assignedWhitelistVault,
    balance: budget.balance.toString(),
    maxReleaseNet: budget.maxReleaseNet.toString(),
    pendingNet: budget.pendingNet.toString(),
    panicLocked: budget.panicLocked,
    dailyReleaseRate: budget.dailyReleaseRate.toString(),
    dailyFeeRate: budget.dailyFeeRate.toString(),
  };
}

export async function executeOperation(rawInput, dependencies = {}) {
  const input = requireObject(rawInput);
  if (typeof input.operation !== "string" || !SUPPORTED_OPERATIONS.has(input.operation)) {
    throw new Error("Gateway operation is not supported.");
  }
  const chainId = requireChainId(input.chainId);
  const deployment = getProductionDeployment(chainId);
  const client =
    dependencies.client ??
    createNoTiltClient({
      deployment,
    });

  if (input.operation === "verify-deployment") {
    const result = await client.verifyDeployment();
    return {
      chainId,
      chain: deployment.key,
      result: Object.fromEntries(
        Object.entries(result).map(([key, value]) => [
          key,
          typeof value === "bigint" ? value.toString() : value,
        ]),
      ),
    };
  }

  const agent = requireAddress(input.agent, "agent");
  if (input.operation === "resolve-assignment") {
    const vault = await client.readProtocol(
      "registry",
      "officialWhitelistVault",
      [agent],
    );
    return {
      chainId,
      chain: deployment.key,
      agent,
      assignedVault: requireAddress(String(vault), "assignedVault"),
      active: String(vault).toLowerCase() !== zeroAddress,
    };
  }

  const vault = requireAddress(input.vault, "vault");
  if (input.operation === "read-vault") {
    const budgets = await Promise.all(
      deployment.assets.map(async (asset) =>
        serializeBudget(
          await client.getAgentBudget(vault, agent, asset.address),
          asset,
        ),
      ),
    );
    return {
      chainId,
      chain: deployment.key,
      vault,
      agent,
      budgets,
    };
  }

  if (input.operation === "prepare-release-execution") {
    const transaction = await client.buildAgentReleaseExecution({
      vault,
      agent,
      requestId: requireRequestId(input.requestId),
    });
    return serializeUnsignedTransaction(transaction);
  }

  if (input.operation === "prepare-release-cancellation") {
    const transaction = await client.buildAgentReleaseCancellation({
      vault,
      agent,
      requestId: requireRequestId(input.requestId),
    });
    return serializeUnsignedTransaction(transaction);
  }

  const asset = requireAsset(deployment, input.asset);
  const amount = requireAmount(input.amount, asset.decimals);
  if (input.operation === "prepare-deposit") {
    const transactions = await client.prepareDepositTransactions({
      depositor: agent,
      vault,
      asset: asset.address,
      amount,
    });
    return {
      chainId,
      chain: deployment.key,
      vault,
      agent,
      asset: asset.symbol,
      amount: input.amount,
      transactions: transactions.map(serializeUnsignedTransaction),
    };
  }

  const transaction = await client.buildAgentReleaseRequest({
    vault,
    agent,
    asset: asset.address,
    netAmount: amount,
  });
  return {
    chainId,
    chain: deployment.key,
    vault,
    agent,
    asset: asset.symbol,
    amount: input.amount,
    transaction: serializeUnsignedTransaction(transaction),
  };
}

function safeMessage(error) {
  const message = error instanceof Error ? error.message : String(error);
  return message.replace(/[\r\n]+/g, " ").slice(0, 400);
}

async function main() {
  try {
    let rawInput = "";
    for await (const chunk of process.stdin) {
      rawInput += chunk;
    }
    const input = JSON.parse(rawInput);
    const data = await executeOperation(input);
    process.stdout.write(JSON.stringify({ ok: true, data }));
  } catch (error) {
    process.stdout.write(
      JSON.stringify({
        ok: false,
        error: {
          code: "NOTILT_GATEWAY_REJECTED",
          message: safeMessage(error),
        },
      }),
    );
    process.exitCode = 1;
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  await main();
}
