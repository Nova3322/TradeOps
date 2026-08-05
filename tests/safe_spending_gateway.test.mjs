import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import { executeOperation } from "../src/trading_control_plane/safe_spending_gateway/index.mjs";

const SAFE="0x1111111111111111111111111111111111111111";
const DELEGATE="0x2222222222222222222222222222222222222222";
const RECIPIENT="0x3333333333333333333333333333333333333333";
const HASH=`0x${"ab".repeat(32)}`;

function client({enabled=true,limit=100_000_000n,spent=20_000_000n,balance=90_000_000n}={}) {
  return {
    async readContract({functionName}) {
      if(functionName==="isModuleEnabled") return enabled;
      if(functionName==="getTokenAllowance") return [limit,spent,1440n,0n,7n];
      if(functionName==="balanceOf") return balance;
      if(functionName==="generateTransferHash") return HASH;
      throw new Error(`unexpected ${functionName}`);
    },
    async getBlock(){return {number:123n,timestamp:456n};},
    async getBytecode(){return "0x1234";},
  };
}

const base={chainId:42161,rpcUrl:"https://example.invalid",safe:SAFE,delegate:DELEGATE,asset:"USDC"};

test("reads the official Arbitrum Safe allowance without signing",async()=>{
  const value=await executeOperation({operation:"read-limit",...base},{client:client()});
  assert.equal(value.moduleEnabled,true);
  assert.equal(value.available,"80000000");
  assert.equal(value.nonce,"7");
});

test("builds an exact human signature request and never calldata or broadcast",async()=>{
  const value=await executeOperation({operation:"prepare-spend",...base,recipient:RECIPIENT,amount:"25"},{client:client()});
  assert.equal(value.kind,"SAFE_ALLOWANCE_SIGNATURE_REQUEST");
  assert.equal(value.transferHash,HASH);
  assert.equal(value.signing,false);
  assert.equal(value.broadcast,false);
  assert.equal(value.calldataReady,false);
});

test("fails closed for disabled module and over-limit amount",async()=>{
  await assert.rejects(()=>executeOperation({operation:"prepare-spend",...base,recipient:RECIPIENT,amount:"25"},{client:client({enabled:false})}),/not enabled/);
  await assert.rejects(()=>executeOperation({operation:"prepare-spend",...base,recipient:RECIPIENT,amount:"81"},{client:client()}),/exceeds/);
});

test("builds an exact unsigned USDC deposit to the configured Safe",async()=>{
  const value=await executeOperation({operation:"prepare-deposit",chainId:42161,rpcUrl:"https://example.invalid",safe:SAFE,sender:RECIPIENT,asset:"USDC",amount:"12.5"},{client:client()});
  assert.equal(value.kind,"SAFE_ERC20_DEPOSIT_UNSIGNED_TRANSACTION");
  assert.equal(value.safe,SAFE);
  assert.equal(value.amount,"12500000");
  assert.equal(value.to.toLowerCase(),"0xaf88d065e77c8cc2239327c5edb3a432268e5831");
  assert.match(value.data,/^0xa9059cbb/);
  assert.equal(value.signing,false);
  assert.equal(value.broadcast,false);
});

test("agent boundary contains no signer, private key, wallet client or broadcast",()=>{
  const source=fs.readFileSync(new URL("../src/trading_control_plane/safe_spending_gateway/index.mjs",import.meta.url),"utf8");
  const forbidden = [
    ["private", "KeyToAccount"],
    ["create", "WalletClient"],
    ["write", "Contract("],
    ["send", "Transaction("],
    ["execute", "Transaction("],
  ].map(parts=>parts.join(""));
  for(const marker of forbidden) assert.equal(source.includes(marker),false,marker);
});
