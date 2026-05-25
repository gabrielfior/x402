"""End-to-end test for ERC-8004 extension against Anvil Prague."""

import subprocess
import time

import pytest
from eth_account import Account
from web3 import Web3

from x402.extensions.erc8004 import (
    ERC8004Config,
    ERCFeedbackClient,
    FeedbackParams,
    create_erc8004_resource_server_extension,
)


@pytest.fixture(scope="module")
def anvil():
    """Start Anvil with Prague hardfork."""
    proc = subprocess.Popen(
        ["anvil", "--hardfork", "Prague", "--chain-id", "1337"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    time.sleep(2)
    yield "http://127.0.0.1:8545"
    proc.terminate()
    proc.wait()


@pytest.fixture
def w3(anvil):
    return Web3(Web3.HTTPProvider(anvil))


@pytest.fixture
def accounts(w3):
    return [Account.from_key(key) for key in [
        "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80",
    ]]


@pytest.mark.skip(reason="requires Foundry + Anvil Prague + contract deployment")
def test_full_flow(anvil, w3, accounts):
    server_acct = accounts[0]
    client_acct = Account.create()

    # Fund client
    w3.eth.send_transaction({
        "to": client_acct.address,
        "value": w3.to_wei(1, "ether"),
        "from": server_acct.address,
    })

    # Deploy FeedbackGateway with mock IdentityRegistry
    # (simplified — in real test, deploy MockIdentityRegistry + set owner)
    # ... deploy code ...

    config = ERC8004Config(
        network="eip155:1337",
        feedback_gateway="0x...",
        reputation_registry="0x...",
        rpc_url=anvil,
    )

    feedback_client = ERCFeedbackClient(config, client_acct)

    # Test check_duplicate
    assert not feedback_client.check_duplicate(1)

    # Test submit_feedback (would need deployed contract)
    # params = FeedbackParams(agent_id=42, value=95, ...)
    # ticket = FeedbackTicket(...)
    # tx_hash = feedback_client.submit_feedback(params, ticket)
    # assert tx_hash.startswith("0x")
