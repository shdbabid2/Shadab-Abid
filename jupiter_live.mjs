#!/usr/bin/env node
/**
 * Live Jupiter Swap V2 executor.
 *
 * Requires:
 *   JUPITER_API_KEY
 *   BS58_PRIVATE_KEY
 *
 * The private key is read only from the environment. Never commit it.
 */
import { Keypair, VersionedTransaction } from "@solana/web3.js";
import bs58 from "bs58";

const BASE_URL = "https://api.jup.ag/swap/v2";

function arg(name) {
  const i = process.argv.indexOf(name);
  if (i < 0 || i + 1 >= process.argv.length) {
    throw new Error(`Missing ${name}`);
  }
  return process.argv[i + 1];
}

const inputMint = arg("--input-mint");
const outputMint = arg("--output-mint");
const amount = arg("--amount");

const apiKey = process.env.JUPITER_API_KEY;
const privateKey = process.env.BS58_PRIVATE_KEY;

if (!apiKey) throw new Error("Missing JUPITER_API_KEY");
if (!privateKey) throw new Error("Missing BS58_PRIVATE_KEY");

const wallet = Keypair.fromSecretKey(bs58.decode(privateKey));

const params = new URLSearchParams({
  inputMint,
  outputMint,
  amount,
  taker: wallet.publicKey.toString(),
  // Conservative custom slippage cap. Can make thin-token swaps fail.
  slippageBps: process.env.LIVE_SLIPPAGE_BPS || "100",
});

const orderResponse = await fetch(`${BASE_URL}/order?${params}`, {
  headers: {"x-api-key": apiKey},
});

if (!orderResponse.ok) {
  throw new Error(`/order ${orderResponse.status}: ${await orderResponse.text()}`);
}

const order = await orderResponse.json();

if (!order.transaction) {
  throw new Error(`No executable transaction: ${JSON.stringify(order)}`);
}

const transaction = VersionedTransaction.deserialize(
  Buffer.from(order.transaction, "base64")
);
transaction.sign([wallet]);

const signedTransaction = Buffer.from(
  transaction.serialize()
).toString("base64");

const executeResponse = await fetch(`${BASE_URL}/execute`, {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    "x-api-key": apiKey,
  },
  body: JSON.stringify({
    signedTransaction,
    requestId: order.requestId,
    ...(order.lastValidBlockHeight
      ? {lastValidBlockHeight: order.lastValidBlockHeight}
      : {}),
  }),
});

if (!executeResponse.ok) {
  throw new Error(
    `/execute ${executeResponse.status}: ${await executeResponse.text()}`
  );
}

const result = await executeResponse.json();
process.stdout.write(JSON.stringify(result));
