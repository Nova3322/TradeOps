// Safe Allowance Module read/preflight boundary. No signer or wallet client is imported.
import { getAllowanceModuleDeployment } from "@safe-global/safe-modules-deployments";
import { createPublicClient, encodeFunctionData, getAddress, http, isAddress, parseUnits, zeroAddress } from "viem";
import { arbitrum } from "viem/chains";
import { pathToFileURL } from "node:url";

const USDC = { symbol: "USDC", address: "0xaf88d065e77c8cC2239327C5EDb3A432268e5831", decimals: 6 };
const safeAbi = [{type:"function",name:"isModuleEnabled",stateMutability:"view",inputs:[{name:"module",type:"address"}],outputs:[{type:"bool"}]}];
const erc20Abi = [
  {type:"function",name:"balanceOf",stateMutability:"view",inputs:[{name:"account",type:"address"}],outputs:[{type:"uint256"}]},
  {type:"function",name:"transfer",stateMutability:"nonpayable",inputs:[{name:"recipient",type:"address"},{name:"amount",type:"uint256"}],outputs:[{type:"bool"}]},
];

function address(value, field) { if (typeof value !== "string" || !isAddress(value)) throw new Error(`${field} must be an EVM address.`); return getAddress(value); }
function rpc(value) { const parsed = new URL(value); if (parsed.protocol !== "https:") throw new Error("Safe RPC must use HTTPS."); return parsed.toString(); }
function deployment() { const item = getAllowanceModuleDeployment({network:"42161"}) ?? getAllowanceModuleDeployment(); const module = item?.networkAddresses?.["42161"]; if (!item || !module) throw new Error("Official Safe Allowance Module is unavailable for Arbitrum."); return {abi:item.abi,module:getAddress(module),version:item.version}; }
function amount(value) { if (typeof value !== "string") throw new Error("amount must be a decimal string."); const parsed=parseUnits(value,USDC.decimals); if(parsed<=0n) throw new Error("amount must be positive."); return parsed; }

async function facts(client, safe, delegate) {
  const trusted=deployment();
  const [moduleEnabled, allowance, balance, block] = await Promise.all([
    client.readContract({address:safe,abi:safeAbi,functionName:"isModuleEnabled",args:[trusted.module]}),
    client.readContract({address:trusted.module,abi:trusted.abi,functionName:"getTokenAllowance",args:[safe,delegate,USDC.address]}),
    client.readContract({address:USDC.address,abi:erc20Abi,functionName:"balanceOf",args:[safe]}),
    client.getBlock(),
  ]);
  const [limit, spent, resetTimeMinutes, lastResetMinutes, nonce]=allowance;
  const available=limit>spent?limit-spent:0n;
  return {trusted,moduleEnabled,limit,spent,available,resetTimeMinutes,lastResetMinutes,nonce,balance,block};
}

export async function executeOperation(input,{client:givenClient}={}) {
  if (!input || !["read-limit","prepare-spend","prepare-deposit"].includes(input.operation) || Number(input.chainId)!==42161) throw new Error("Safe gateway operation is unsupported.");
  if (input.asset!=="USDC") throw new Error("Only trusted Arbitrum USDC is supported.");
  const safe=address(input.safe,"safe");
  const client=givenClient ?? createPublicClient({chain:arbitrum,transport:http(rpc(input.rpcUrl),{timeout:15000})});
  if(input.operation==="prepare-deposit") {
    const requested=amount(input.amount), sender=address(input.sender,"sender");
    const [safeCode,block]=await Promise.all([client.getBytecode({address:safe}),client.getBlock()]);
    if(!safeCode || safeCode==="0x") throw new Error("Safe account is not deployed on Arbitrum.");
    return {kind:"SAFE_ERC20_DEPOSIT_UNSIGNED_TRANSACTION",chainId:42161,safe,sender,token:USDC.address,asset:USDC.symbol,amount:requested.toString(),to:USDC.address,value:"0",data:encodeFunctionData({abi:erc20Abi,functionName:"transfer",args:[safe,requested]}),blockNumber:block.number.toString(),blockTimestamp:block.timestamp.toString(),signing:false,broadcast:false,nextStep:"Human-owned wallet reviews and submits this exact USDC transfer to the configured Safe."};
  }
  const delegate=address(input.delegate,"delegate");
  const value=await facts(client,safe,delegate);
  const base={chainId:42161,module:value.trusted.module,moduleVersion:value.trusted.version,safe,delegate,token:USDC.address,asset:USDC.symbol,moduleEnabled:Boolean(value.moduleEnabled),limit:value.limit.toString(),spent:value.spent.toString(),available:value.available.toString(),balance:value.balance.toString(),resetTimeMinutes:Number(value.resetTimeMinutes),lastResetMinutes:Number(value.lastResetMinutes),nonce:value.nonce.toString(),blockNumber:value.block.number.toString(),blockTimestamp:value.block.timestamp.toString()};
  if(input.operation==="read-limit") return base;
  const recipient=address(input.recipient,"recipient"), requested=amount(input.amount);
  if(!value.moduleEnabled) throw new Error("Safe Allowance Module is not enabled.");
  if(requested>value.available || requested>value.balance) throw new Error("Requested amount exceeds the current Safe spending limit or balance.");
  const transferHash=await client.readContract({address:value.trusted.module,abi:value.trusted.abi,functionName:"generateTransferHash",args:[safe,USDC.address,recipient,requested,zeroAddress,0n,value.nonce]});
  const data=encodeFunctionData({
    abi:value.trusted.abi,
    functionName:"executeAllowanceTransfer",
    args:[safe,USDC.address,recipient,requested,zeroAddress,0n,delegate,"0x"],
  });
  return {kind:"SAFE_ALLOWANCE_SIGNATURE_REQUEST",...base,recipient,amount:requested.toString(),transferHash,from:delegate,to:value.trusted.module,value:"0",data,signing:false,broadcast:false,calldataReady:true,nextStep:"The connected delegate wallet reviews and submits this exact Allowance Module transaction."};
}

async function main(){try{let raw="";for await(const chunk of process.stdin)raw+=chunk;const data=await executeOperation(JSON.parse(raw));process.stdout.write(JSON.stringify({ok:true,data}));}catch(error){process.stdout.write(JSON.stringify({ok:false,error:{code:"SAFE_GATEWAY_REJECTED",message:String(error?.message||error).replace(/[\r\n]+/g," ").slice(0,400)}}));process.exitCode=1;}}
if(process.argv[1]&&import.meta.url===pathToFileURL(process.argv[1]).href)await main();
