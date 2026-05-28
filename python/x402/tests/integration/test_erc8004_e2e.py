"""End-to-end test for the gateway-less ERC-8004 extension against Anvil."""

import subprocess
import time

import pytest
from eth_account import Account
from web3 import Web3

from x402.extensions.erc8004 import (
    ERC8004Config,
    ERCFeedbackClient,
    FeedbackParams,
    InMemoryUploader,
    verify_integrity,
)
from x402.schemas.payments import PaymentPayload, PaymentRequirements


@pytest.fixture(scope="module")
def anvil():
    proc = subprocess.Popen(
        ["anvil", "--hardfork", "Prague", "--chain-id", "1337"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    time.sleep(2)
    yield "http://127.0.0.1:8545"
    proc.terminate()
    proc.wait()


@pytest.mark.integration
def test_artifact_published_and_integrity_holds(anvil) -> None:
    """Build+publish an artifact and confirm the hosted bytes match feedbackHash.

    Exercises the off-chain binding path without requiring a deployed
    ReputationRegistry on the local chain.
    """
    w3 = Web3(Web3.HTTPProvider(anvil))
    signer = Account.from_key(
        "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
    )
    config = ERC8004Config(
        network=f"eip155:{w3.eth.chain_id}",
        reputation_registry="0x" + "00" * 20,
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
    payload = PaymentPayload(payload={"sig": "0xdead"}, accepted=requirements)
    params = FeedbackParams(agent_id=42, value=90, endpoint="/weather")
    up = InMemoryUploader()

    uri, feedback_hash, updated = client.build_and_publish_artifact(
        requirements=requirements,
        payment_payload=payload,
        tx_hash="0x" + "ab" * 32,
        payer=signer.address,
        payment_method="eip3009",
        request={"method": "GET", "url": "https://x/y", "headerDigest": "0x" + "00" * 32, "bodyDigest": "0x" + "00" * 32},
        response={"status": 200, "headerDigest": "0x" + "00" * 32, "bodyDigest": "0x" + "0a" * 32},
        params=params,
        uploader=up,
        receipt=None,
    )
    assert verify_integrity(up.store[uri], feedback_hash) is True
    assert updated.feedback_uri == uri
