"""Real E2E: upload feedback artifact to IPFS via Pinata, then submit
ReputationRegistry.giveFeedback on a local Anvil instance.

Requires:
- `anvil` on PATH (Foundry)
- PINATA_JWT in the repo-root .env (or environment)

Run it explicitly (note -s so the CID prints to your console):

    cd python/x402 && uv run pytest tests/integration/test_erc8004_pinata_e2e.py -v -s -m integration
"""

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
    verify_integrity,
)
from x402.schemas.payments import PaymentPayload, PaymentRequirements

# Anvil dev account #0 (well-known key, pre-funded on a fresh Anvil).
ANVIL_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"

# Minimal "registry" runtime: log full calldata (LOG0) then return success.
# Lets any selector (incl. giveFeedback) succeed and makes the calldata
# inspectable on-chain. Deploy bytecode = constructor that returns the runtime.
#   runtime:  CALLDATACOPY(0,0,size); LOG0(0,size); RETURN(0,0)
MOCK_REGISTRY_DEPLOY = "0x600f80600b6000396000f3366000600037366000a060006000f3"


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


def _deploy_mock_registry(w3: Web3, deployer: Account) -> str:
    base_fee = w3.eth.get_block("latest")["baseFeePerGas"]
    tx = {
        "type": 2,
        "chainId": w3.eth.chain_id,
        "nonce": w3.eth.get_transaction_count(deployer.address),
        "data": MOCK_REGISTRY_DEPLOY,
        "value": 0,
        "gas": 200000,
        "maxFeePerGas": w3.eth.max_priority_fee + 2 * base_fee,
        "maxPriorityFeePerGas": w3.eth.max_priority_fee,
    }
    signed = deployer.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
    assert receipt["contractAddress"], "mock registry deploy failed"
    return receipt["contractAddress"]


@pytest.mark.integration
def test_real_pinata_upload_then_onchain_feedback(anvil) -> None:
    jwt = _load_pinata_jwt()
    if not jwt:
        pytest.skip("PINATA_JWT not set (.env or environment)")

    w3 = Web3(Web3.HTTPProvider(anvil))
    signer = Account.from_key(ANVIL_KEY)

    registry_addr = _deploy_mock_registry(w3, signer)
    print(f"\n[e2e] mock ReputationRegistry deployed at {registry_addr}")

    config = ERC8004Config(
        network=f"eip155:{w3.eth.chain_id}",
        reputation_registry=registry_addr,
        identity_registry="0x" + "00" * 20,
        rpc_url=anvil,
    )
    client = ERCFeedbackClient(config, signer)

    requirements = PaymentRequirements(
        scheme="exact",
        network=f"eip155:{w3.eth.chain_id}",
        asset="0x" + "01" * 20,
        amount="1000000",
        pay_to="0x" + "03" * 20,
        max_timeout_seconds=60,
    )
    payload = PaymentPayload(payload={"sig": "0xdeadbeef"}, accepted=requirements)
    params = FeedbackParams(agent_id=42, value=95, tag1="x402", tag2="weather", endpoint="/weather")

    # ---- Real IPFS upload via Pinata ----
    uploader = PinataUploader(jwt=jwt)
    uri, feedback_hash, params = client.build_and_publish_artifact(
        requirements=requirements,
        payment_payload=payload,
        tx_hash="0x" + "ab" * 32,
        payer=signer.address,
        payment_method="eip3009",
        request={"method": "GET", "url": "https://example.com/weather", "headerDigest": "0x" + "00" * 32, "bodyDigest": "0x" + "00" * 32},
        response={"status": 200, "headerDigest": "0x" + "00" * 32, "bodyDigest": "0x" + "0a" * 32},
        params=params,
        uploader=uploader,
        receipt=None,
    )

    print(f"[e2e] uploaded artifact to IPFS")
    print(f"[e2e] CID:          {uploader.last_cid}")
    print(f"[e2e] feedbackURI:  {uri}")
    print(f"[e2e] feedbackHash: 0x{feedback_hash.hex()}")
    print(f"[e2e] gateway:      https://gateway.pinata.cloud/ipfs/{uploader.last_cid}")

    assert uploader.last_cid, "no CID returned by Pinata"
    assert uri == f"ipfs://{uploader.last_cid}"

    # ---- Real on-chain submission on Anvil ----
    onchain_tx = client.submit_feedback_to_registry(params)
    receipt = w3.eth.wait_for_transaction_receipt(onchain_tx)
    print(f"[e2e] giveFeedback tx: {onchain_tx} (status={receipt['status']})")

    assert receipt["status"] == 1, "on-chain giveFeedback reverted"
    assert receipt["to"] and Web3.to_checksum_address(receipt["to"]) == Web3.to_checksum_address(registry_addr)

    # The mock logged the full calldata; confirm the CID + feedbackHash are in it.
    assert receipt["logs"], "no log emitted by registry"
    calldata = bytes(receipt["logs"][0]["data"])
    assert feedback_hash in calldata, "feedbackHash not found in on-chain calldata"
    assert uploader.last_cid.encode() in calldata, "CID (feedbackURI) not found in on-chain calldata"

    print("[e2e] verified: CID + feedbackHash present in on-chain giveFeedback calldata")
    print(f"\n>>> IPFS CID: {uploader.last_cid}\n")
