"""Runnable ERC-8004 feedback demo: real IPFS upload + on-chain giveFeedback.

This is a standalone, non-pytest version of the integration test. It:
  1. starts a local Anvil instance,
  2. deploys three tiny mock contracts (ERC-20 token, IdentityRegistry,
     ReputationRegistry) so no Solidity compiler is needed,
  3. performs a real settlement transfer (real txHash + Transfer log),
  4. signs a real agent InteractionReceipt over {settlement, request, response},
  5. uploads the canonical artifact to REAL IPFS via Pinata (prints the CID),
  6. submits ReputationRegistry.giveFeedback on-chain (prints the decoded tx),
  7. verifies the whole thing off-chain (verify_feedback -> FULL).

Requirements:
  - Foundry (`anvil` on PATH)        https://book.getfoundry.sh
  - PINATA_JWT in the repo-root .env (or environment)

Run (from the repo root, using the project venv):

    cd python/x402
    uv run python ../../examples/python/clients/erc8004/main.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from eth_account import Account
from web3 import Web3

from x402.extensions.erc8004 import (
    ERC8004Config,
    ERCFeedbackClient,
    FeedbackParams,
    PinataUploader,
    TrustTier,
    create_interaction_receipt,
    verify_feedback,
    verify_settlement,
)
from x402.extensions.erc8004.client import REPUTATION_ABI
from x402.schemas.payments import PaymentPayload, PaymentRequirements

# Anvil dev account #0 (well-known key, pre-funded on a fresh Anvil).
ANVIL_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"

# --- Hand-assembled mock contracts (no solc needed) ---
# ReputationRegistry: LOG0(full calldata) then return success.
MOCK_REGISTRY_DEPLOY = "0x600f80600b6000396000f3366000600037366000a060006000f3"
# ERC-20-ish token: emit Transfer(caller, calldata[4:36], calldata[36:68]); return success.
_TRANSFER_TOPIC = "ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
MOCK_TOKEN_DEPLOY = (
    "0x603680600b6000396000f3"
    "60206024600037"   # CALLDATACOPY(0, 0x24, 0x20) -> mem[0:32] = amount
    "60043533"         # PUSH1 4; CALLDATALOAD; CALLER -> [to, from]
    "7f" + _TRANSFER_TOPIC
    + "60206000a3"     # LOG3(0, 32, topic0, from, to)
    + "60006000f3"     # RETURN(0, 0)
)


def _identity_registry_deploy(owner_addr: str) -> str:
    owner_hex = owner_addr.lower().removeprefix("0x")
    runtime = "73" + owner_hex + "60005260206000f3"  # PUSH20 owner; MSTORE(0); RETURN(0,32)
    return "0x601d80600b6000396000f3" + runtime


def _load_pinata_jwt() -> str | None:
    jwt = os.getenv("PINATA_JWT")
    if jwt:
        return jwt
    env_path = Path(__file__).resolve().parents[4] / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("PINATA_JWT="):
                return line.split("=", 1)[1].strip()
    return None


def _send(w3: Web3, signer: Account, tx: dict):
    base_fee = w3.eth.get_block("latest")["baseFeePerGas"]
    tx = {
        **tx,
        "type": 2,
        "chainId": w3.eth.chain_id,
        "nonce": w3.eth.get_transaction_count(signer.address),
        "maxFeePerGas": w3.eth.max_priority_fee + 2 * base_fee,
        "maxPriorityFeePerGas": w3.eth.max_priority_fee,
    }
    signed = signer.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    return w3.eth.wait_for_transaction_receipt(tx_hash)


def _deploy(w3: Web3, signer: Account, bytecode: str) -> str:
    receipt = _send(w3, signer, {"data": bytecode, "value": 0, "gas": 300000})
    assert receipt["contractAddress"], "deploy failed"
    return receipt["contractAddress"]


def _transfer(w3: Web3, signer: Account, token: str, to: str, amount: int) -> str:
    calldata = (
        bytes.fromhex("a9059cbb")
        + bytes.fromhex(to.lower().removeprefix("0x")).rjust(32, b"\x00")
        + amount.to_bytes(32, "big")
    )
    receipt = _send(
        w3, signer, {"to": Web3.to_checksum_address(token), "data": "0x" + calldata.hex(), "value": 0, "gas": 200000}
    )
    assert receipt["status"] == 1, "settlement transfer reverted"
    h = receipt["transactionHash"].hex()
    return h if h.startswith("0x") else "0x" + h


class _CapturingPinata(PinataUploader):
    content: bytes = b""

    def upload(self, content: bytes) -> str:
        self.content = content
        return super().upload(content)


def main() -> int:
    if shutil.which("anvil") is None:
        print("ERROR: `anvil` not found on PATH. Install Foundry: https://book.getfoundry.sh")
        return 1
    jwt = _load_pinata_jwt()
    if not jwt:
        print("ERROR: PINATA_JWT not set (add it to the repo-root .env or export it).")
        return 1

    proc = subprocess.Popen(
        ["anvil", "--hardfork", "Prague", "--chain-id", "31337"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    url = "http://127.0.0.1:8545"
    try:
        w3 = Web3(Web3.HTTPProvider(url))
        for _ in range(50):
            try:
                if w3.is_connected():
                    break
            except Exception:
                pass
            time.sleep(0.1)
        else:
            print("ERROR: could not connect to Anvil at", url)
            return 1

        chain_id = w3.eth.chain_id
        payer = Account.from_key(ANVIL_KEY)  # client / payer
        agent = Account.create()             # agent owner key (signs the receipt)

        token = _deploy(w3, payer, MOCK_TOKEN_DEPLOY)
        identity_registry = _deploy(w3, payer, _identity_registry_deploy(agent.address))
        reputation_registry = _deploy(w3, payer, MOCK_REGISTRY_DEPLOY)
        print(f"token={token}")
        print(f"identityRegistry={identity_registry} (ownerOf -> {agent.address})")
        print(f"reputationRegistry={reputation_registry}")

        pay_to = agent.address
        amount = 1_000_000

        settlement_tx = _transfer(w3, payer, token, pay_to, amount)
        print(f"settlement txHash={settlement_tx}")

        requirements = PaymentRequirements(
            scheme="exact",
            network=f"eip155:{chain_id}",
            asset=Web3.to_checksum_address(token),
            amount=str(amount),
            pay_to=Web3.to_checksum_address(pay_to),
            max_timeout_seconds=60,
        )
        payload = PaymentPayload(payload={"sig": "0xdeadbeef"}, accepted=requirements)
        request = {"method": "GET", "url": "https://example.com/weather", "headerDigest": "0x" + "00" * 32, "bodyDigest": "0x" + "00" * 32}
        response = {"status": 200, "headerDigest": "0x" + "00" * 32, "bodyDigest": "0x" + "0a" * 32}

        # Agent signs the receipt over {settlement, request, response}.
        receipt = create_interaction_receipt(
            agent,
            agent_id=42,
            requirements=requirements,
            payment_payload=payload,
            tx_hash=settlement_tx,
            payer=payer.address,
            request=request,
            response=response,
            payment_method="eip3009",
        )
        print(f"agent receipt signed by {agent.address} (covers request+response)")

        config = ERC8004Config(
            network=f"eip155:{chain_id}",
            reputation_registry=reputation_registry,
            identity_registry=identity_registry,
            rpc_url=url,
        )
        client = ERCFeedbackClient(config, payer)
        params = FeedbackParams(agent_id=42, value=95, tag1="x402", tag2="weather", endpoint="/weather")
        uploader = _CapturingPinata(jwt=jwt)

        uri, feedback_hash, params = client.build_and_publish_artifact(
            requirements=requirements,
            payment_payload=payload,
            tx_hash=settlement_tx,
            payer=payer.address,
            payment_method="eip3009",
            request=request,
            response=response,
            params=params,
            uploader=uploader,
            receipt=receipt,
        )
        print(f"\nCID:          {uploader.last_cid}")
        print(f"feedbackURI:  {uri}")
        print(f"feedbackHash: 0x{feedback_hash.hex()}")
        print(f"gateway:      https://{uploader.last_cid}.ipfs.inbrowser.link/")

        onchain_tx = client.submit_feedback_to_registry(params)
        fb_receipt = w3.eth.wait_for_transaction_receipt(onchain_tx)

        tx = w3.eth.get_transaction(onchain_tx)
        _, args = w3.eth.contract(abi=REPUTATION_ABI).decode_function_input(tx["input"])
        print("\n===== on-chain feedback transaction (Anvil) =====")
        print(f"  txHash:        {onchain_tx}")
        print(f"  status:        {fb_receipt['status']} (block {fb_receipt['blockNumber']})")
        print(f"  from (client): {tx['from']}")
        print(f"  to (registry): {tx['to']}")
        print(f"  giveFeedback.agentId:      {args['agentId']}")
        print(f"  giveFeedback.value:        {args['value']}")
        print(f"  giveFeedback.feedbackURI:  {args['feedbackURI']}")
        print(f"  giveFeedback.feedbackHash: 0x{args['feedbackHash'].hex()}")
        print("=================================================")

        assert fb_receipt["status"] == 1
        assert args["feedbackURI"] == uri
        assert args["feedbackHash"] == feedback_hash
        assert verify_settlement(w3, json.loads(uploader.content)) is True
        tier = verify_feedback(
            w3,
            identity_registry,
            uploader.content,
            feedback_hash,
            json.loads(uploader.content),
            submitter=tx["from"],
        )
        print(f"verify_feedback -> {tier.name}")
        assert tier == TrustTier.FULL

        print(f"\nSUCCESS — feedback posted on-chain, artifact at ipfs://{uploader.last_cid}")
        return 0
    finally:
        proc.terminate()
        proc.wait()


if __name__ == "__main__":
    sys.exit(main())
