// Signing-free NoTilt SDK boundary used by the Python control plane.
import {
  createNoTiltClient,
  formatAssetAmount,
  getContractAbi,
  getProductionDeployment,
  parseAssetAmount,
  serializeUnsignedTransaction,
} from "@notilt/sdk";
import {
  decodeFunctionData,
  getAddress,
  isAddress,
  parseEventLogs,
  zeroAddress,
} from "viem";
import { pathToFileURL } from "node:url";

const SUPPORTED_CHAIN_IDS = new Set([1, 56, 42161]);
const RECEIPT_KINDS = new Set([
  "DEPOSIT",
  "RELEASE_REQUEST",
  "RELEASE_EXECUTION",
  "RELEASE_CANCELLATION",
]);
const SUPPORTED_OPERATIONS = new Set([
  "resolve-assignment",
  "verify-deployment",
  "read-vault",
  "prepare-deposit",
  "prepare-release-request",
  "prepare-release-execution",
  "prepare-release-cancellation",
  "verify-receipt",
]);
const vaultAbi = getContractAbi("vault");

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

function requireTransactionHash(value) {
  if (typeof value !== "string" || !/^0x[0-9a-fA-F]{64}$/.test(value)) {
    throw new Error("transactionHash must be a 32-byte transaction hash.");
  }
  return value;
}

function requireReceiptKind(value) {
  if (typeof value !== "string" || !RECEIPT_KINDS.has(value)) {
    throw new Error("receiptKind is not supported.");
  }
  return value;
}

function requireConfirmations(value) {
  const confirmations = Number(value);
  if (!Number.isInteger(confirmations) || confirmations < 1 || confirmations > 128) {
    throw new Error("minConfirmations must be an integer from 1 through 128.");
  }
  return confirmations;
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

function sameAddress(left, right) {
  return getAddress(left).toLowerCase() === getAddress(right).toLowerCase();
}

function matchingEvent(receipt, vault, eventName) {
  const parsed = parseEventLogs({
    abi: vaultAbi,
    logs: receipt.logs.filter((log) => sameAddress(log.address, vault)),
    eventName,
    strict: true,
  });
  if (parsed.length !== 1) {
    throw new Error(`Receipt must contain exactly one ${eventName} event from the Vault.`);
  }
  return parsed[0];
}

async function verifyReceipt(client, deployment, input) {
  const receiptKind = requireReceiptKind(input.receiptKind);
  const transactionHash = requireTransactionHash(input.transactionHash);
  const minConfirmations = requireConfirmations(input.minConfirmations);
  const vault = requireAddress(input.vault, "vault");
  const agent = requireAddress(input.agent, "agent");
  const [transaction, receipt, latestBlock] = await Promise.all([
    client.publicClient.getTransaction({ hash: transactionHash }),
    client.publicClient.getTransactionReceipt({ hash: transactionHash }),
    client.publicClient.getBlock(),
  ]);
  if (receipt.status !== "success") {
    throw new Error("NoTilt transaction reverted.");
  }
  if (
    !sameAddress(transaction.from, agent) ||
    !transaction.to ||
    !sameAddress(transaction.to, vault)
  ) {
    throw new Error("Receipt transaction sender or Vault target does not match.");
  }
  if (transaction.chainId !== undefined && Number(transaction.chainId) !== deployment.chainId) {
    throw new Error("Receipt transaction belongs to a different chain.");
  }
  if (
    receipt.transactionHash.toLowerCase() !== transactionHash.toLowerCase() ||
    receipt.blockNumber > latestBlock.number
  ) {
    throw new Error("Receipt identity or block height is invalid.");
  }
  const confirmations = latestBlock.number - receipt.blockNumber + 1n;
  if (confirmations < BigInt(minConfirmations)) {
    throw new Error(
      `Receipt has ${confirmations} confirmations; ${minConfirmations} are required.`,
    );
  }
  const receiptBlock = await client.publicClient.getBlock({
    blockNumber: receipt.blockNumber,
  });
  const decoded = decodeFunctionData({ abi: vaultAbi, data: transaction.input });
  const base = {
    receiptKind,
    chainId: deployment.chainId,
    chain: deployment.key,
    transactionHash,
    vault,
    agent,
    blockNumber: receipt.blockNumber.toString(),
    blockTimestamp: receiptBlock.timestamp.toString(),
    confirmations: confirmations.toString(),
  };

  if (receiptKind === "RELEASE_EXECUTION" || receiptKind === "RELEASE_CANCELLATION") {
    const expectedFunction =
      receiptKind === "RELEASE_EXECUTION"
        ? "executeWhitelistRelease"
        : "cancelWhitelistRelease";
    const eventName =
      receiptKind === "RELEASE_EXECUTION"
        ? "WhitelistReleaseExecuted"
        : "WhitelistReleaseCancelled";
    const requestId = requireRequestId(input.requestId);
    if (
      decoded.functionName !== expectedFunction ||
      decoded.args?.length !== 1 ||
      String(decoded.args[0]).toLowerCase() !== requestId.toLowerCase()
    ) {
      throw new Error(`Receipt is not the expected ${expectedFunction} call.`);
    }
    const event = matchingEvent(receipt, vault, eventName);
    if (String(event.args.requestId).toLowerCase() !== requestId.toLowerCase()) {
      throw new Error(`${eventName} request identity does not match.`);
    }
    return { ...base, requestId };
  }

  const asset = requireAsset(deployment, input.asset);
  const amount = requireAmount(input.amount, asset.decimals);
  if (receiptKind === "DEPOSIT") {
    if (
      decoded.functionName !== "deposit" ||
      decoded.args?.length !== 2 ||
      !sameAddress(String(decoded.args[0]), asset.address) ||
      BigInt(decoded.args[1]) !== amount
    ) {
      throw new Error("Receipt is not the expected NoTilt deposit call.");
    }
    const expectedValue = asset.native ? amount : 0n;
    if (transaction.value !== expectedValue) {
      throw new Error("Deposit transaction native value does not match.");
    }
    const event = matchingEvent(receipt, vault, "Deposit");
    if (
      !sameAddress(event.args.vault, vault) ||
      !sameAddress(event.args.asset, asset.address) ||
      !sameAddress(event.args.from, agent) ||
      event.args.requestedAmount !== amount ||
      event.args.creditedAmount !== amount
    ) {
      throw new Error("Deposit event does not match the authorized transfer.");
    }
    return {
      ...base,
      asset: asset.symbol,
      requestedAmount: formatAssetAmount(amount, asset.decimals),
      creditedAmount: formatAssetAmount(event.args.creditedAmount, asset.decimals),
    };
  }

  if (
    decoded.functionName !== "requestWhitelistRelease" ||
    decoded.args?.length !== 2 ||
    !sameAddress(String(decoded.args[0]), asset.address) ||
    BigInt(decoded.args[1]) !== amount ||
    transaction.value !== 0n
  ) {
    throw new Error("Receipt is not the expected NoTilt release request call.");
  }
  const event = matchingEvent(receipt, vault, "WhitelistReleaseRequested");
  if (
    !sameAddress(event.args.requester, agent) ||
    !sameAddress(event.args.asset, asset.address) ||
    event.args.netAmount !== amount
  ) {
    throw new Error("Whitelist release request event does not match.");
  }
  return {
    ...base,
    asset: asset.symbol,
    requestId: event.args.requestId,
    netAmount: formatAssetAmount(event.args.netAmount, asset.decimals),
    fee: formatAssetAmount(event.args.fee, asset.decimals),
    executeAfter: event.args.executeAfter.toString(),
    expiresAt: event.args.expiresAt.toString(),
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

  if (input.operation === "verify-receipt") {
    return verifyReceipt(client, deployment, input);
  }

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
