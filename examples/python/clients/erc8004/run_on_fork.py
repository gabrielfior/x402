"""Run the ERC-8004 feedback flow against an EXISTING chain (e.g. a Tenderly
mainnet fork) where the ERC-8004 registries are already deployed.

Unlike `main.py` (which spawns Anvil and deploys mock contracts), this script
assumes the ReputationRegistry / IdentityRegistry / ERC-20 asset already exist
at the addresses you pass in. It:

  1. (optionally) performs a real ERC-20 transfer payer -> payTo for settlement,
  2. signs a real agent InteractionReceipt over {settlement, request, response},
  3. uploads the canonical artifact to REAL IPFS via Pinata (prints the CID),
  4. submits ReputationRegistry.giveFeedback on the fork (prints the decoded tx),
  5. verifies the whole thing off-chain (verify_feedback -> tier).

Everything is configured via environment variables — see the block below.

Run:

    cd python/x402
    uv pip install -e .            # one-time
    RPC_URL=... PAYER_PRIVATE_KEY=... \
    IDENTITY_REGISTRY=0x... ASSET=0x... \
    uv run python ../../examples/python/clients/erc8004/run_on_fork.py
"""

from __future__ import annotations

import json
import os
import sys
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
from x402.extensions.erc8004.constants import MAINNET_REPUTATION_REGISTRY
from x402.schemas.payments import PaymentPayload, PaymentRequirements

# Minimal ERC-20 ABI for the settlement transfer + balance checks.
ERC20_ABI = [
    {"name": "transfer", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "to", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}]},
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "a", "type": "address"}], "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "decimals", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint8"}]},
]

# ERC-8004 IdentityRegistry: register() mints an agent owned by msg.sender and
# emits Registered(agentId, tokenURI, owner). It's also an ERC-721 (mint emits
# Transfer(0x0, owner, tokenId)) which we use as a fallback to read the new id.
IDENTITY_REGISTER_ABI = [
    {"name": "register", "type": "function", "stateMutability": "nonpayable",
     "inputs": [], "outputs": [{"name": "agentId", "type": "uint256"}]},
    {"name": "ownerOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "id", "type": "uint256"}], "outputs": [{"name": "", "type": "address"}]},
    {"anonymous": False, "name": "Registered", "type": "event", "inputs": [
        {"indexed": True, "name": "agentId", "type": "uint256"},
        {"indexed": False, "name": "tokenURI", "type": "string"},
        {"indexed": True, "name": "owner", "type": "address"}]},
    {"anonymous": False, "name": "Transfer", "type": "event", "inputs": [
        {"indexed": True, "name": "from", "type": "address"},
        {"indexed": True, "name": "to", "type": "address"},
        {"indexed": True, "name": "tokenId", "type": "uint256"}]},
]


def _env(name: str, default: str | None = None, required: bool = False) -> str | None:
    val = os.getenv(name, default)
    if required and not val:
        print(f"ERROR: ${name} is required.")
        sys.exit(1)
    return val


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


def _fund_gas_if_low(w3: Web3, payer: Account, to: str, min_eth: float = 0.02, topup_eth: float = 0.05) -> None:
    """Top up `to` with ETH from `payer` if it can't cover its own gas."""
    addr = Web3.to_checksum_address(to)
    if w3.eth.get_balance(addr) >= w3.to_wei(min_eth, "ether"):
        return
    print(f"funding agent {addr} with {topup_eth} ETH for registration gas...")
    rcpt = _send(w3, payer, {"to": addr, "value": w3.to_wei(topup_eth, "ether"), "gas": 21000})
    if rcpt["status"] != 1:
        raise RuntimeError("gas funding transfer to the agent failed")


def _agent_id_from_receipt(w3: Web3, addr: str, owner: str, rcpt: Any) -> int:
    """Read the new agentId from a register() receipt (Registered, else ERC-721 mint)."""
    from web3.logs import DISCARD

    c = w3.eth.contract(address=addr, abi=IDENTITY_REGISTER_ABI)
    regs = c.events.Registered().process_receipt(rcpt, errors=DISCARD)
    if regs:
        return int(regs[0]["args"]["agentId"])
    for ev in c.events.Transfer().process_receipt(rcpt, errors=DISCARD):
        if int(ev["args"]["from"], 16) == 0 and Web3.to_checksum_address(ev["args"]["to"]) == owner:
            return int(ev["args"]["tokenId"])
    raise RuntimeError("register() succeeded but no Registered/mint event found to read agentId")


def _register_agent(w3: Web3, signer: Account, identity_registry: str) -> int:
    """Register a fresh agent owned by `signer`; return its agentId.

    Tries register() first, then register(string) with a tokenURI, dry-running
    each via eth_call so a revert surfaces a clear reason instead of a traceback.
    """
    from eth_abi import encode as abi_encode
    from eth_utils import function_signature_to_4byte_selector as selector

    addr = Web3.to_checksum_address(identity_registry)
    owner = Web3.to_checksum_address(signer.address)
    attempts = [
        ("register()", selector("register()")),
        (
            'register("ipfs://x402-erc8004-demo")',
            selector("register(string)") + abi_encode(["string"], ["ipfs://x402-erc8004-demo"]),
        ),
    ]

    last_err: str | None = None
    for label, data in attempts:
        try:
            w3.eth.call({"from": owner, "to": addr, "data": data})
        except Exception as e:  # reverted — try the next overload
            last_err = f"{label}: {e}"
            print(f"  {label} dry-run reverted, trying next variant...")
            continue
        rcpt = _send(w3, signer, {"to": addr, "data": "0x" + data.hex(), "value": 0, "gas": 600000})
        if rcpt["status"] != 1:
            raise RuntimeError(f"{label} reverted on-chain (tx {rcpt['transactionHash'].hex()})")
        return _agent_id_from_receipt(w3, addr, owner, rcpt)

    raise RuntimeError(
        "register() reverted for all variants on this IdentityRegistry; "
        f"last reason: {last_err}. The deployed registry may require EIP-7702 "
        "delegated registration, a fee, or a different signature."
    )


class _CapturingPinata(PinataUploader):
    content: bytes = b""

    def upload(self, content: bytes) -> str:
        self.content = content
        return super().upload(content)


def main() -> int:
    # --- config ---
    rpc_url = _env("RPC_URL", required=True)
    jwt = _load_pinata_jwt()
    if not jwt:
        print("ERROR: PINATA_JWT not set (repo-root .env or environment).")
        return 1

    payer_key = _env("PAYER_PRIVATE_KEY", required=True)
    agent_key = _env("AGENT_PRIVATE_KEY")  # owner of agentId; generated if absent
    reputation_registry = _env("REPUTATION_REGISTRY", MAINNET_REPUTATION_REGISTRY)
    identity_registry = _env("IDENTITY_REGISTRY", required=True)
    asset = _env("ASSET", required=True)
    agent_id = int(_env("AGENT_ID", "1"))
    amount = int(_env("AMOUNT", "1000000"))
    payment_method = _env("PAYMENT_METHOD", "eip3009")
    existing_tx = _env("SETTLEMENT_TX_HASH")

    w3 = Web3(Web3.HTTPProvider(rpc_url))
    if not w3.is_connected():
        print(f"ERROR: cannot connect to RPC at {rpc_url}")
        return 1
    chain_id = w3.eth.chain_id

    payer = Account.from_key(payer_key)
    register_agent = _env("REGISTER_AGENT", "1") not in ("0", "false", "False", "")

    if register_agent:
        # The ReputationRegistry forbids self-feedback (client must NOT be the
        # agent owner/operator). So the AGENT is a separate EOA: it registers
        # itself (owns agentId, becomes payTo, signs the receipt) while the PAYER
        # is the distinct client that pays and submits feedback. The payer funds
        # the agent's gas, so you only have to fund one account (the payer).
        agent = Account.from_key(agent_key) if agent_key else Account.create()
        if agent.address.lower() == payer.address.lower():
            print("ERROR: AGENT must differ from PAYER — the registry forbids self-feedback.")
            return 1
        _fund_gas_if_low(w3, payer, agent.address)
        print(f"\nregistering a new agent (owner = {agent.address})...")
        agent_id = _register_agent(w3, agent, identity_registry)
        pay_to = Web3.to_checksum_address(agent.address)
        print(f"registered agentId = {agent_id} (owner = {agent.address})")
    else:
        agent = Account.from_key(agent_key) if agent_key else Account.create()
        pay_to = Web3.to_checksum_address(_env("AGENT_PAYTO", agent.address))

    print(f"chainId            = {chain_id}")
    print(f"payer (client)     = {payer.address}")
    print(f"agent owner        = {agent.address}"
          + ("" if (register_agent or agent_key) else "  (GENERATED — see note below)"))
    print(f"payTo              = {pay_to}")
    print(f"asset (ERC-20)     = {asset}")
    print(f"reputationRegistry = {reputation_registry}")
    print(f"identityRegistry   = {identity_registry}")
    print(f"agentId            = {agent_id}")

    # --- pre-flight: ownerOf(agentId) must == payTo for TrustTier.FULL ---
    try:
        owner = (
            w3.eth.contract(
                address=Web3.to_checksum_address(identity_registry),
                abi=[{"name": "ownerOf", "type": "function", "stateMutability": "view",
                      "inputs": [{"name": "id", "type": "uint256"}],
                      "outputs": [{"name": "", "type": "address"}]}],
            ).functions.ownerOf(agent_id).call()
        )
        print(f"ownerOf(agentId)   = {owner}")
        if Web3.to_checksum_address(owner) != pay_to:
            print("  NOTE: ownerOf(agentId) != payTo -> verify_feedback will be REJECTED.")
            print("        The giveFeedback tx still lands on-chain; only the FULL")
            print("        trust-tier check needs payTo == ownerOf(agentId) and the")
            print("        receipt signed by that owner key. Set AGENT_PRIVATE_KEY to")
            print("        the owner's key (and AGENT_PAYTO to its address), or register")
            print(f"        agentId {agent_id} -> {pay_to} on the IdentityRegistry.")
    except Exception as e:
        print(f"  WARN: ownerOf(agentId) call failed ({e}); continuing.")

    token = w3.eth.contract(address=Web3.to_checksum_address(asset), abi=ERC20_ABI)

    # --- settlement transfer (or reuse an existing tx) ---
    if existing_tx:
        settlement_tx = existing_tx if existing_tx.startswith("0x") else "0x" + existing_tx
        print(f"\nusing existing settlement txHash = {settlement_tx}")
    else:
        gas_bal = w3.eth.get_balance(payer.address)
        tok_bal = token.functions.balanceOf(payer.address).call()
        print(f"\npayer ETH balance  = {w3.from_wei(gas_bal, 'ether')} ETH")
        print(f"payer token bal    = {tok_bal} (need {amount})")
        if gas_bal == 0:
            print("ERROR: payer has no ETH for gas. Fund it on the fork, then re-run.")
            return 1
        if tok_bal < amount:
            print("ERROR: payer token balance < amount. Fund it on the fork, then re-run.")
            return 1
        print("sending settlement transfer...")
        data = token.functions.transfer(pay_to, amount).build_transaction({"from": payer.address})["data"]
        rcpt = _send(w3, payer, {"to": Web3.to_checksum_address(asset), "data": data, "value": 0, "gas": 120000})
        if rcpt["status"] != 1:
            print("ERROR: settlement transfer reverted.")
            return 1
        h = rcpt["transactionHash"].hex()
        settlement_tx = h if h.startswith("0x") else "0x" + h
        print(f"settlement txHash  = {settlement_tx}")

    # --- build x402 payment objects matching the transfer ---
    requirements = PaymentRequirements(
        scheme="exact",
        network=f"eip155:{chain_id}",
        asset=Web3.to_checksum_address(asset),
        amount=str(amount),
        pay_to=pay_to,
        max_timeout_seconds=60,
    )
    payload = PaymentPayload(payload={"sig": "0xdeadbeef"}, accepted=requirements)
    request = {"method": "GET", "url": "https://example.com/weather",
               "headerDigest": "0x" + "00" * 32, "bodyDigest": "0x" + "00" * 32}
    response = {"status": 200, "headerDigest": "0x" + "00" * 32, "bodyDigest": "0x" + "0a" * 32}

    # --- agent signs the receipt over {settlement, request, response} ---
    receipt = create_interaction_receipt(
        agent,
        requirements=requirements,
        payment_payload=payload,
        tx_hash=settlement_tx,
        payer=payer.address,
        request=request,
        response=response,
        payment_method=payment_method,
    )
    print(f"agent receipt signed by {agent.address}")

    # --- build + REAL IPFS upload ---
    config = ERC8004Config(
        network=f"eip155:{chain_id}",
        reputation_registry=reputation_registry,
        identity_registry=identity_registry,
        rpc_url=rpc_url,
    )
    client = ERCFeedbackClient(config, payer)
    params = FeedbackParams(agent_id=agent_id, value=95, tag1="x402", tag2="weather", endpoint="/weather")
    uploader = _CapturingPinata(jwt=jwt)

    uri, feedback_hash, params = client.build_and_publish_artifact(
        requirements=requirements,
        payment_payload=payload,
        tx_hash=settlement_tx,
        payer=payer.address,
        payment_method=payment_method,
        request=request,
        response=response,
        params=params,
        uploader=uploader,
        receipt=receipt,
    )
    print(f"\nCID                = {uploader.last_cid}")
    print(f"feedbackURI        = {uri}")
    print(f"feedbackHash       = 0x{feedback_hash.hex()}")
    print(f"gateway            = https://{uploader.last_cid}.ipfs.inbrowser.link/")

    # --- REAL on-chain giveFeedback ---
    onchain_tx = client.submit_feedback_to_registry(params)
    fb_receipt = w3.eth.wait_for_transaction_receipt(onchain_tx)
    tx = w3.eth.get_transaction(onchain_tx)
    _, args = w3.eth.contract(abi=REPUTATION_ABI).decode_function_input(tx["input"])
    print("\n===== on-chain feedback transaction =====")
    print(f"  txHash:        {onchain_tx}")
    print(f"  status:        {fb_receipt['status']} (block {fb_receipt['blockNumber']})")
    print(f"  from (client): {tx['from']}")
    print(f"  to (registry): {tx['to']}")
    print(f"  giveFeedback.agentId:      {args['agentId']}")
    print(f"  giveFeedback.value:        {args['value']}")
    print(f"  giveFeedback.feedbackURI:  {args['feedbackURI']}")
    print(f"  giveFeedback.feedbackHash: 0x{args['feedbackHash'].hex()}")
    print("=========================================")

    if fb_receipt["status"] != 1:
        print("ERROR: giveFeedback reverted on-chain.")
        return 1

    # --- off-chain verification against the fork state ---
    artifact = json.loads(uploader.content)
    print(f"\nverify_settlement -> {verify_settlement(w3, artifact)}")
    tier = verify_feedback(w3, identity_registry, uploader.content, feedback_hash, artifact)
    print(f"verify_feedback   -> {tier.name}")

    print(f"\nDONE — feedback posted on-chain, artifact at ipfs://{uploader.last_cid}")
    return 0 if tier != TrustTier.REJECTED else 0


if __name__ == "__main__":
    sys.exit(main())
