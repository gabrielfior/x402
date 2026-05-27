"""Shared logic for the ERC-8004 feedback demos (run_on_fork / run_on_anvil).

Both demos run the same end-to-end flow against a chain where the ERC-8004
registries already exist:

  1. (optionally) register a fresh agent EOA (owns agentId, becomes payTo),
  2. settle a payment (plain ERC-20 transfer, or DAI via x402 Permit2 proxy),
  3. sign an agent InteractionReceipt over {settlement, request, response},
  4. upload the canonical artifact to REAL IPFS via Pinata,
  5. submit ReputationRegistry.giveFeedback on-chain,
  6. verify the whole thing off-chain (verify_feedback -> TrustTier).

The only difference between the two entry points is how they get a chain and
how they fund the payer:
  - run_on_fork: talks to an RPC directly; the payer must already be funded.
  - run_on_anvil: forks the RPC into a local Anvil subprocess and funds the
    payer by impersonating whales (see `fund_erc20_from_whale`).

`run_erc8004_demo` takes an optional `fund_token` hook so run_on_anvil can top
up ERC-20 balances before each settlement leg.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

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
from x402.mechanisms.evm.constants import (
    PERMIT2_ADDRESS,
    X402_EXACT_PERMIT2_PROXY_ABI,
    X402_EXACT_PERMIT2_PROXY_ADDRESS,
)
from x402.mechanisms.evm.exact.permit2_utils import _build_permit2_settle_args, create_permit2_payload
from x402.mechanisms.evm.signers import EthAccountSigner
from x402.mechanisms.evm.types import ExactPermit2Payload
from x402.schemas.payments import PaymentPayload, PaymentRequirements

# Canonical DAI on Ethereum mainnet (override with `DAI_ASSET` on other forks).
MAINNET_DAI = "0x6B175474E89094C44Da98b954Eedeac495271d0F"

# Minimal ERC-20 ABI for the settlement transfer + balance checks.
ERC20_ABI = [
    {"name": "transfer", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "to", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}]},
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "a", "type": "address"}], "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "decimals", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint8"}]},
    {"name": "allowance", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "approve", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}]},
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


def get_env(name: str, default: str | None = None, required: bool = False) -> str | None:
    val = os.getenv(name, default)
    if required and not val:
        print(f"ERROR: ${name} is required.")
        sys.exit(1)
    return val


def load_pinata_jwt() -> str | None:
    jwt = os.getenv("PINATA_JWT")
    if jwt:
        return jwt
    env_path = Path(__file__).resolve().parents[4] / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("PINATA_JWT="):
                return line.split("=", 1)[1].strip()
    return None


def send_tx(w3: Web3, signer: Account, tx: dict) -> Any:
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


def fund_gas_if_low(w3: Web3, payer: Account, to: str, min_eth: float = 0.02, topup_eth: float = 0.05) -> None:
    """Top up `to` with ETH from `payer` if it can't cover its own gas."""
    addr = Web3.to_checksum_address(to)
    if w3.eth.get_balance(addr) >= w3.to_wei(min_eth, "ether"):
        return
    print(f"funding agent {addr} with {topup_eth} ETH for registration gas...")
    rcpt = send_tx(w3, payer, {"to": addr, "value": w3.to_wei(topup_eth, "ether"), "gas": 21000})
    if rcpt["status"] != 1:
        raise RuntimeError("gas funding transfer to the agent failed")


def agent_id_from_receipt(w3: Web3, addr: str, owner: str, rcpt: Any) -> int:
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


def register_agent(w3: Web3, signer: Account, identity_registry: str) -> int:
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
        rcpt = send_tx(w3, signer, {"to": addr, "data": "0x" + data.hex(), "value": 0, "gas": 600000})
        if rcpt["status"] != 1:
            raise RuntimeError(f"{label} reverted on-chain (tx {rcpt['transactionHash'].hex()})")
        return agent_id_from_receipt(w3, addr, owner, rcpt)

    raise RuntimeError(
        "register() reverted for all variants on this IdentityRegistry; "
        f"last reason: {last_err}. The deployed registry may require EIP-7702 "
        "delegated registration, a fee, or a different signature."
    )


def fund_erc20_from_whale(
    w3: Web3, token: str, recipient: str, needed: int, whale: str, gas_eth: float = 10.0
) -> None:
    """On a local Anvil fork, top up `recipient` with `token` by impersonating a whale.

    No-op if the recipient already holds at least `needed`. Uses
    anvil_setBalance + anvil_impersonateAccount so the whale can sign and pay gas.
    """
    from eth_abi import encode as abi_encode
    from eth_utils import function_signature_to_4byte_selector as selector

    token_cs = Web3.to_checksum_address(token)
    recipient_cs = Web3.to_checksum_address(recipient)
    whale_cs = Web3.to_checksum_address(whale)
    erc20 = w3.eth.contract(address=token_cs, abi=ERC20_ABI)

    bal = int(erc20.functions.balanceOf(recipient_cs).call())
    if bal >= needed:
        print(f"  recipient already holds {bal} of {token_cs} (need {needed}); no whale funding")
        return

    whale_bal = int(erc20.functions.balanceOf(whale_cs).call())
    if whale_bal < needed:
        raise RuntimeError(
            f"whale {whale_cs} holds {whale_bal} of {token_cs}, < needed {needed}. "
            "Set ASSET_WHALE / DAI_WHALE to an address with a larger balance."
        )

    w3.provider.make_request("anvil_setBalance", [whale_cs, hex(w3.to_wei(gas_eth, "ether"))])
    w3.provider.make_request("anvil_impersonateAccount", [whale_cs])
    try:
        data = selector("transfer(address,uint256)") + abi_encode(
            ["address", "uint256"], [recipient_cs, needed]
        )
        tx_hash = w3.eth.send_transaction(
            {"from": whale_cs, "to": token_cs, "data": "0x" + data.hex(), "value": 0, "gas": 200000}
        )
        rcpt = w3.eth.wait_for_transaction_receipt(tx_hash)
        if rcpt["status"] != 1:
            raise RuntimeError("whale ERC-20 transfer reverted")
    finally:
        w3.provider.make_request("anvil_stopImpersonatingAccount", [whale_cs])
    print(f"  funded {recipient_cs} with {needed} of {token_cs} from whale {whale_cs}")


def ensure_dai_permit2_allowance(w3: Web3, payer: Account, dai: str, amount: int) -> None:
    """Ensure `payer` has approved Uniswap Permit2 to spend at least `amount` DAI."""
    token = w3.eth.contract(address=Web3.to_checksum_address(dai), abi=ERC20_ABI)
    permit2 = Web3.to_checksum_address(PERMIT2_ADDRESS)
    cur = int(token.functions.allowance(payer.address, permit2).call())
    if cur >= amount:
        return
    print(f"approving Permit2 on DAI (allowance was {cur}, need {amount})...")
    data = token.functions.approve(permit2, 2**256 - 1).build_transaction({"from": payer.address})["data"]
    rcpt = send_tx(w3, payer, {"to": Web3.to_checksum_address(dai), "data": data, "value": 0, "gas": 120000})
    if rcpt["status"] != 1:
        raise RuntimeError("DAI approve(Permit2) reverted")


def settle_dai_via_x402_permit2_proxy(
    w3: Web3, payer: Account, requirements: PaymentRequirements, inner_payload: dict[str, Any]
) -> str:
    """Execute `x402ExactPermit2Proxy.settle` using a signed `create_permit2_payload` dict."""
    payload_obj = ExactPermit2Payload.from_dict(inner_payload)
    permit_tuple, owner_addr, witness_tuple, sig_bytes = _build_permit2_settle_args(payload_obj)
    proxy = Web3.to_checksum_address(X402_EXACT_PERMIT2_PROXY_ADDRESS)
    c = w3.eth.contract(address=proxy, abi=X402_EXACT_PERMIT2_PROXY_ABI)
    func = c.functions.settle(permit_tuple, owner_addr, witness_tuple, sig_bytes)
    gas = int(func.estimate_gas({"from": payer.address}) * 1.25)
    data = func.build_transaction({"from": payer.address})["data"]
    rcpt = send_tx(
        w3,
        payer,
        {"to": proxy, "data": data, "value": 0, "gas": min(max(gas, 250000), 2_000_000)},
    )
    if rcpt["status"] != 1:
        raise RuntimeError("x402ExactPermit2Proxy.settle reverted")
    h = rcpt["transactionHash"].hex()
    return h if h.startswith("0x") else "0x" + h


class CapturingPinata(PinataUploader):
    """PinataUploader that also retains the exact bytes it uploaded."""

    content: bytes = b""

    def upload(self, content: bytes) -> str:
        self.content = content
        return super().upload(content)


def run_feedback_cycle(
    *,
    w3: Web3,
    payer: Account,
    agent: Account,
    agent_id: int,
    chain_id: int,
    settlement_tx: str,
    requirements: PaymentRequirements,
    payment_payload: PaymentPayload,
    payment_method: str,
    reputation_registry: str,
    identity_registry: str,
    rpc_url: str,
    jwt: str,
    request: dict[str, Any],
    response: dict[str, Any],
    feedback_tag2: str,
    print_gateway: bool = False,
) -> int:
    """Build artifact, upload to Pinata, giveFeedback, verify. Returns 0 on FULL."""
    receipt = create_interaction_receipt(
        agent,
        agent_id=agent_id,
        requirements=requirements,
        payment_payload=payment_payload,
        tx_hash=settlement_tx,
        payer=payer.address,
        request=request,
        response=response,
        payment_method=payment_method,
    )
    print(f"agent receipt signed by {agent.address} ({feedback_tag2})")

    config = ERC8004Config(
        network=f"eip155:{chain_id}",
        reputation_registry=reputation_registry,
        identity_registry=identity_registry,
        rpc_url=rpc_url,
    )
    client = ERCFeedbackClient(config, payer)
    params = FeedbackParams(
        agent_id=agent_id,
        value=95,
        tag1="x402",
        tag2=feedback_tag2,
        endpoint="/weather",
    )
    uploader = CapturingPinata(jwt=jwt)

    uri, feedback_hash, params = client.build_and_publish_artifact(
        requirements=requirements,
        payment_payload=payment_payload,
        tx_hash=settlement_tx,
        payer=payer.address,
        payment_method=payment_method,
        request=request,
        response=response,
        params=params,
        uploader=uploader,
        receipt=receipt,
    )
    print(f"\nCID                = {uploader.last_cid}  ({feedback_tag2})")
    print(f"feedbackURI        = {uri}")
    print(f"feedbackHash       = 0x{feedback_hash.hex()}")
    if print_gateway and uploader.last_cid:
        print(f"gateway            = https://{uploader.last_cid}.ipfs.inbrowser.link/")

    onchain_tx = client.submit_feedback_to_registry(params)
    fb_receipt = w3.eth.wait_for_transaction_receipt(onchain_tx)
    tx = w3.eth.get_transaction(onchain_tx)
    _, args = w3.eth.contract(abi=REPUTATION_ABI).decode_function_input(tx["input"])
    print(f"\n===== on-chain feedback ({feedback_tag2}) =====")
    print(f"  txHash:        {onchain_tx}")
    print(f"  status:        {fb_receipt['status']} (block {fb_receipt['blockNumber']})")
    print(f"  from (client): {tx['from']}")
    print(f"  giveFeedback.feedbackURI:  {args['feedbackURI']}")
    print(f"  giveFeedback.feedbackHash: 0x{args['feedbackHash'].hex()}")
    print("=========================================")

    if fb_receipt["status"] != 1:
        print("ERROR: giveFeedback reverted on-chain.")
        return 1

    artifact = json.loads(uploader.content)
    print(f"\nverify_settlement -> {verify_settlement(w3, artifact)}")
    tier = verify_feedback(w3, identity_registry, uploader.content, feedback_hash, artifact, submitter=tx["from"])
    print(f"verify_feedback   -> {tier.name}")
    return 0 if tier == TrustTier.FULL else 1


@dataclass
class DemoConfig:
    rpc_url: str
    jwt: str
    payer_key: str
    agent_key: str | None
    reputation_registry: str
    identity_registry: str
    asset: str
    amount: int
    payment_method: str
    existing_tx: str | None
    register_agent_flag: bool
    agent_id_default: int
    agent_payto: str | None
    run_dai: bool
    dai_asset: str
    dai_amount: int
    dai_timeout: int


def parse_config() -> DemoConfig:
    """Read all demo configuration from environment variables."""
    rpc_url = get_env("RPC_URL", required=True)
    jwt = load_pinata_jwt()
    if not jwt:
        print("ERROR: PINATA_JWT not set (repo-root .env or environment).")
        sys.exit(1)

    amount = int(get_env("AMOUNT", "1000000"))
    run_dai = (get_env("RUN_DAI_PERMIT2_SCENARIO", "0") or "").strip().lower() in ("1", "true", "yes", "on")
    return DemoConfig(
        rpc_url=rpc_url,
        jwt=jwt,
        payer_key=get_env("PAYER_PRIVATE_KEY", required=True),
        agent_key=get_env("AGENT_PRIVATE_KEY"),
        reputation_registry=get_env("REPUTATION_REGISTRY", MAINNET_REPUTATION_REGISTRY),
        identity_registry=get_env("IDENTITY_REGISTRY", required=True),
        asset=get_env("ASSET", required=True),
        amount=amount,
        payment_method=get_env("PAYMENT_METHOD", "eip3009"),
        existing_tx=get_env("SETTLEMENT_TX_HASH"),
        register_agent_flag=get_env("REGISTER_AGENT", "1") not in ("0", "false", "False", ""),
        agent_id_default=int(get_env("AGENT_ID", "1")),
        agent_payto=get_env("AGENT_PAYTO"),
        run_dai=run_dai,
        dai_asset=Web3.to_checksum_address(get_env("DAI_ASSET") or MAINNET_DAI),
        dai_amount=int((get_env("DAI_PERMIT2_AMOUNT") or str(amount)).strip()),
        dai_timeout=int((get_env("DAI_PERMIT2_MAX_TIMEOUT", "3600") or "3600").strip()),
    )


def run_erc8004_demo(
    *,
    w3: Web3,
    cfg: DemoConfig,
    effective_rpc_url: str,
    fund_token: Callable[[str, int], None] | None = None,
) -> int:
    """Drive the full flow. `effective_rpc_url` is what the feedback client uses
    (the local Anvil URL for run_on_anvil). `fund_token(token_addr, needed)` is
    an optional hook to top up ERC-20 balances before settlement (whale funding).
    """
    chain_id = w3.eth.chain_id
    payer = Account.from_key(cfg.payer_key)

    if cfg.register_agent_flag:
        # The ReputationRegistry forbids self-feedback (client must NOT be the
        # agent owner/operator). So the AGENT is a separate EOA: it registers
        # itself (owns agentId, becomes payTo, signs the receipt) while the PAYER
        # is the distinct client that pays and submits feedback. The payer funds
        # the agent's gas, so you only have to fund one account (the payer).
        agent = Account.from_key(cfg.agent_key) if cfg.agent_key else Account.create()
        if agent.address.lower() == payer.address.lower():
            print("ERROR: AGENT must differ from PAYER — the registry forbids self-feedback.")
            return 1
        fund_gas_if_low(w3, payer, agent.address)
        print(f"\nregistering a new agent (owner = {agent.address})...")
        agent_id = register_agent(w3, agent, cfg.identity_registry)
        pay_to = Web3.to_checksum_address(agent.address)
        print(f"registered agentId = {agent_id} (owner = {agent.address})")
    else:
        agent = Account.from_key(cfg.agent_key) if cfg.agent_key else Account.create()
        agent_id = cfg.agent_id_default
        pay_to = Web3.to_checksum_address(cfg.agent_payto or agent.address)

    print(f"chainId            = {chain_id}")
    print(f"payer (client)     = {payer.address}")
    print(f"agent owner        = {agent.address}"
          + ("" if (cfg.register_agent_flag or cfg.agent_key) else "  (GENERATED — see note below)"))
    print(f"payTo              = {pay_to}")
    print(f"asset (ERC-20)     = {cfg.asset}")
    print(f"reputationRegistry = {cfg.reputation_registry}")
    print(f"identityRegistry   = {cfg.identity_registry}")
    print(f"agentId            = {agent_id}")

    # --- pre-flight: ownerOf(agentId) must == payTo for TrustTier.FULL ---
    try:
        owner = (
            w3.eth.contract(
                address=Web3.to_checksum_address(cfg.identity_registry),
                abi=[{"name": "ownerOf", "type": "function", "stateMutability": "view",
                      "inputs": [{"name": "id", "type": "uint256"}],
                      "outputs": [{"name": "", "type": "address"}]}],
            ).functions.ownerOf(agent_id).call()
        )
        print(f"ownerOf(agentId)   = {owner}")
        if Web3.to_checksum_address(owner) != pay_to:
            print("  NOTE: ownerOf(agentId) != payTo -> verify_feedback will be REJECTED.")
            print("        Set AGENT_PRIVATE_KEY to the owner's key (and AGENT_PAYTO to")
            print(f"        its address), or register agentId {agent_id} -> {pay_to}.")
    except Exception as e:
        print(f"  WARN: ownerOf(agentId) call failed ({e}); continuing.")

    token = w3.eth.contract(address=Web3.to_checksum_address(cfg.asset), abi=ERC20_ABI)

    # --- settlement transfer (or reuse an existing tx) ---
    if cfg.existing_tx:
        settlement_tx = cfg.existing_tx if cfg.existing_tx.startswith("0x") else "0x" + cfg.existing_tx
        print(f"\nusing existing settlement txHash = {settlement_tx}")
    else:
        if fund_token is not None:
            fund_token(cfg.asset, cfg.amount)
        gas_bal = w3.eth.get_balance(payer.address)
        tok_bal = token.functions.balanceOf(payer.address).call()
        print(f"\npayer ETH balance  = {w3.from_wei(gas_bal, 'ether')} ETH")
        print(f"payer token bal    = {tok_bal} (need {cfg.amount})")
        if gas_bal == 0:
            print("ERROR: payer has no ETH for gas. Fund it on the fork, then re-run.")
            return 1
        if tok_bal < cfg.amount:
            print("ERROR: payer token balance < amount. Fund it on the fork, then re-run.")
            return 1
        print("sending settlement transfer...")
        data = token.functions.transfer(pay_to, cfg.amount).build_transaction({"from": payer.address})["data"]
        rcpt = send_tx(w3, payer, {"to": Web3.to_checksum_address(cfg.asset), "data": data, "value": 0, "gas": 120000})
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
        asset=Web3.to_checksum_address(cfg.asset),
        amount=str(cfg.amount),
        pay_to=Web3.to_checksum_address(pay_to),
        max_timeout_seconds=60,
    )
    payload = PaymentPayload(payload={"sig": "0xdeadbeef"}, accepted=requirements)
    request = {"method": "GET", "url": "https://example.com/weather",
               "headerDigest": "0x" + "00" * 32, "bodyDigest": "0x" + "00" * 32}
    response = {"status": 200, "headerDigest": "0x" + "00" * 32, "bodyDigest": "0x" + "0a" * 32}

    rc = run_feedback_cycle(
        w3=w3,
        payer=payer,
        agent=agent,
        agent_id=agent_id,
        chain_id=chain_id,
        settlement_tx=settlement_tx,
        requirements=requirements,
        payment_payload=payload,
        payment_method=cfg.payment_method,
        reputation_registry=cfg.reputation_registry,
        identity_registry=cfg.identity_registry,
        rpc_url=effective_rpc_url,
        jwt=cfg.jwt,
        request=request,
        response=response,
        feedback_tag2="weather",
        print_gateway=True,
    )
    if rc != 0:
        return rc

    if cfg.run_dai:
        rc2 = _run_dai_permit2_scenario(
            w3=w3,
            cfg=cfg,
            chain_id=chain_id,
            payer=payer,
            agent=agent,
            agent_id=agent_id,
            pay_to=pay_to,
            effective_rpc_url=effective_rpc_url,
            request=request,
            response=response,
            fund_token=fund_token,
        )
        if rc2 != 0:
            return rc2

    suffix = " (plain transfer + DAI/Permit2)" if cfg.run_dai else ""
    print(f"\nDONE — feedback posted on-chain{suffix}.")
    return 0


def _run_dai_permit2_scenario(
    *,
    w3: Web3,
    cfg: DemoConfig,
    chain_id: int,
    payer: Account,
    agent: Account,
    agent_id: int,
    pay_to: str,
    effective_rpc_url: str,
    request: dict[str, Any],
    response: dict[str, Any],
    fund_token: Callable[[str, int], None] | None,
) -> int:
    proxy_cs = Web3.to_checksum_address(X402_EXACT_PERMIT2_PROXY_ADDRESS)
    code = w3.eth.get_code(proxy_cs)
    if not code or code == b"\x00":
        print(
            "ERROR: RUN_DAI_PERMIT2_SCENARIO is set but x402ExactPermit2Proxy "
            f"has no code at {proxy_cs} on this chain."
        )
        return 1

    print("\n----- DAI + Permit2 second scenario -----")
    dai = cfg.dai_asset
    dai_amount = cfg.dai_amount
    if fund_token is not None:
        fund_token(dai, dai_amount)
    dai_token = w3.eth.contract(address=dai, abi=ERC20_ABI)
    gas_bal = w3.eth.get_balance(payer.address)
    dai_bal = dai_token.functions.balanceOf(payer.address).call()
    print(f"payer ETH balance  = {w3.from_wei(gas_bal, 'ether')} ETH")
    print(f"payer DAI balance  = {dai_bal} (need {dai_amount})")
    if gas_bal == 0:
        print("ERROR: payer has no ETH for gas (Permit2 leg).")
        return 1
    if dai_bal < dai_amount:
        print("ERROR: payer DAI balance < DAI_PERMIT2_AMOUNT. Fund DAI on the fork.")
        return 1

    ensure_dai_permit2_allowance(w3, payer, dai, dai_amount)

    requirements_dai = PaymentRequirements(
        scheme="exact",
        network=f"eip155:{chain_id}",
        asset=dai,
        amount=str(dai_amount),
        pay_to=Web3.to_checksum_address(pay_to),
        max_timeout_seconds=cfg.dai_timeout,
        extra={"assetTransferMethod": "permit2"},
    )
    signer = EthAccountSigner(payer)
    inner = create_permit2_payload(signer, requirements_dai)
    permit_payment = PaymentPayload(payload=inner, accepted=requirements_dai)

    print("settling DAI via x402ExactPermit2Proxy.settle (Permit2)...")
    try:
        settlement_tx_dai = settle_dai_via_x402_permit2_proxy(w3, payer, requirements_dai, inner)
    except Exception as e:
        print(f"ERROR: Permit2 settle failed: {e}")
        return 1
    print(f"Permit2 settle tx  = {settlement_tx_dai}")

    return run_feedback_cycle(
        w3=w3,
        payer=payer,
        agent=agent,
        agent_id=agent_id,
        chain_id=chain_id,
        settlement_tx=settlement_tx_dai,
        requirements=requirements_dai,
        payment_payload=permit_payment,
        payment_method="permit2",
        reputation_registry=cfg.reputation_registry,
        identity_registry=cfg.identity_registry,
        rpc_url=effective_rpc_url,
        jwt=cfg.jwt,
        request=request,
        response=response,
        feedback_tag2="weather-permit2",
        print_gateway=False,
    )
