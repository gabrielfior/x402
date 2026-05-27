"""Real E2E: upload feedback artifact to IPFS via Pinata, then submit
ReputationRegistry.giveFeedback on a local Anvil instance.

Realistic flow:
- a real ERC-20 settlement transfer on Anvil (real txHash, real Transfer log)
- a real agent-signed InteractionReceipt embedded in the artifact
- artifact uploaded to real IPFS (Pinata), CID printed
- giveFeedback submitted on-chain (real tx)
- full off-chain verification (verify_feedback -> TrustTier.FULL)

Requires:
- `anvil` on PATH (Foundry)
- PINATA_JWT in the repo-root .env (or environment)

Run it explicitly (note -s so the CID prints to your console):

    cd python/x402 && uv run pytest tests/integration/test_erc8004_pinata_e2e.py -v -s -m integration
"""

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
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

# ReputationRegistry: LOG0(full calldata) then return success. Any selector works.
MOCK_REGISTRY_DEPLOY = "0x600f80600b6000396000f3366000600037366000a060006000f3"

# ERC-20-ish token: on any call, emit Transfer(caller, calldata[4:36], calldata[36:68])
# i.e. LOG3 with topic0=keccak(Transfer(address,address,uint256)), topic1=CALLER,
# topic2=`to` arg, data=`amount` arg. Returns success.
_TRANSFER_TOPIC = "ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
_TOKEN_RUNTIME = (
    "60206024600037"   # CALLDATACOPY(0, 0x24, 0x20)  -> mem[0:32] = amount
    "60043533"         # PUSH1 4; CALLDATALOAD; CALLER -> [to, from]
    "7f"               # PUSH32 ...
    + _TRANSFER_TOPIC  # ... Transfer topic
    + "60206000a3"     # PUSH1 0x20; PUSH1 0; LOG3(0, 32, topic0, from, to)
    + "60006000f3"     # RETURN(0, 0)
)
MOCK_TOKEN_DEPLOY = "0x603680600b6000396000f3" + _TOKEN_RUNTIME


def _identity_registry_deploy(owner_addr: str) -> str:
    """Deploy bytecode for a registry whose every call returns `owner_addr`."""
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


@pytest.fixture(scope="module")
def anvil():
    if shutil.which("anvil") is None:
        pytest.skip("anvil not installed")
    proc = subprocess.Popen(
        ["anvil", "--hardfork", "Prague", "--chain-id", "31337"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    url = "http://127.0.0.1:8545"
    w3 = Web3(Web3.HTTPProvider(url))
    for _ in range(50):
        try:
            if w3.is_connected():
                break
        except Exception:
            pass
        time.sleep(0.1)
    yield url
    proc.terminate()
    proc.wait()


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
    """ERC-20 transfer(to, amount); returns the real settlement tx hash."""
    calldata = (
        bytes.fromhex("a9059cbb")
        + bytes.fromhex(to.lower().removeprefix("0x")).rjust(32, b"\x00")
        + amount.to_bytes(32, "big")
    )
    receipt = _send(
        w3, signer, {"to": Web3.to_checksum_address(token), "data": "0x" + calldata.hex(), "value": 0, "gas": 200000}
    )
    assert receipt["status"] == 1, "settlement transfer reverted"
    return receipt["transactionHash"].hex()


class _CapturingPinata(PinataUploader):
    """PinataUploader that also retains the exact bytes it uploaded."""

    content: bytes = b""

    def upload(self, content: bytes) -> str:
        self.content = content
        return super().upload(content)


@pytest.mark.integration
def test_real_settlement_signature_upload_and_feedback(anvil) -> None:
    jwt = _load_pinata_jwt()
    if not jwt:
        pytest.skip("PINATA_JWT not set (.env or environment)")

    w3 = Web3(Web3.HTTPProvider(anvil))
    chain_id = w3.eth.chain_id
    payer = Account.from_key(ANVIL_KEY)        # client / payer
    agent = Account.create()                   # agent owner key (signs the receipt)

    # --- deploy mock contracts ---
    token = _deploy(w3, payer, MOCK_TOKEN_DEPLOY)
    identity_registry = _deploy(w3, payer, _identity_registry_deploy(agent.address))
    reputation_registry = _deploy(w3, payer, MOCK_REGISTRY_DEPLOY)
    print(f"\n[e2e] token={token}")
    print(f"[e2e] identityRegistry={identity_registry} (ownerOf -> {agent.address})")
    print(f"[e2e] reputationRegistry={reputation_registry}")

    pay_to = agent.address  # agent is paid; ownerOf(agentId) == payTo
    amount = 1_000_000

    # --- REAL settlement transfer (real txHash + Transfer log) ---
    settlement_tx = _transfer(w3, payer, token, pay_to, amount)
    if not settlement_tx.startswith("0x"):
        settlement_tx = "0x" + settlement_tx
    print(f"[e2e] settlement txHash={settlement_tx}")

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

    # --- REAL agent signature: server signs the receipt at the HTTP layer over
    #     {version, settlement, request, response} once the response is known ---
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
    print(f"[e2e] agent receipt signed by {agent.address} (covers request+response)")

    # --- build + REAL IPFS upload (Pinata) ---
    config = ERC8004Config(
        network=f"eip155:{chain_id}",
        reputation_registry=reputation_registry,
        identity_registry=identity_registry,
        rpc_url=anvil,
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
    print(f"[e2e] CID:          {uploader.last_cid}")
    print(f"[e2e] feedbackURI:  {uri}")
    print(f"[e2e] feedbackHash: 0x{feedback_hash.hex()}")
    print(f"[e2e] gateway:      https://{uploader.last_cid}.ipfs.inbrowser.link/")

    assert uploader.last_cid
    artifact = json.loads(uploader.content)
    assert artifact["settlement"]["txHash"] == settlement_tx
    assert artifact["interaction"]["response"]["agentSignature"] is not None

    # --- REAL on-chain giveFeedback on Anvil ---
    onchain_tx = client.submit_feedback_to_registry(params)
    fb_receipt = w3.eth.wait_for_transaction_receipt(onchain_tx)
    assert fb_receipt["status"] == 1
    calldata = bytes(fb_receipt["logs"][0]["data"])
    assert feedback_hash in calldata
    assert uploader.last_cid.encode() in calldata

    # Pull the executed tx back from Anvil and decode it, showing it points at
    # the new IPFS entry.
    tx = w3.eth.get_transaction(onchain_tx)
    decoder = w3.eth.contract(abi=REPUTATION_ABI)
    _, args = decoder.decode_function_input(tx["input"])
    print("\n[e2e] ===== on-chain feedback transaction (Anvil) =====")
    print(f"[e2e]   txHash:        {onchain_tx}")
    print(f"[e2e]   status:        {fb_receipt['status']} (block {fb_receipt['blockNumber']})")
    print(f"[e2e]   from (client): {tx['from']}")
    print(f"[e2e]   to (registry): {tx['to']}")
    print(f"[e2e]   giveFeedback.agentId:      {args['agentId']}")
    print(f"[e2e]   giveFeedback.value:        {args['value']}")
    print(f"[e2e]   giveFeedback.feedbackURI:  {args['feedbackURI']}")
    print(f"[e2e]   giveFeedback.feedbackHash: 0x{args['feedbackHash'].hex()}")
    print(f"[e2e]   -> resolves to IPFS:       https://{uploader.last_cid}.ipfs.inbrowser.link/")
    print("[e2e] ================================================")
    assert args["feedbackURI"] == uri == f"ipfs://{uploader.last_cid}"
    assert args["feedbackHash"] == feedback_hash

    # --- full off-chain verification against the real chain state ---
    assert verify_settlement(w3, artifact) is True
    tier = verify_feedback(
        w3,
        identity_registry,
        uploader.content,
        feedback_hash,
        artifact,
        submitter=tx["from"],
    )
    print(f"[e2e] verify_feedback -> {tier.name}")
    assert tier == TrustTier.FULL

    print(f"\n>>> IPFS CID: {uploader.last_cid}\n")
