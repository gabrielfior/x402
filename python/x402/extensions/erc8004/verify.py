"""Verification and dedup for ERC-8004 x402 feedback.

Verification is payment-scheme agnostic: it keys off the universal ERC-20
Transfer event emitted by the asset contract, so EIP-3009, Permit2, and plain
ERC-20 settlements all verify the same way.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Any

from eth_utils import keccak, to_checksum_address

from x402.mechanisms.evm.constants import X402_EXACT_PERMIT2_PROXY_ADDRESS

from .artifact import (
    compute_feedback_hash,
    compute_interaction_hash,
    verify_interaction_receipt,
)
from .types import InteractionReceipt

TRANSFER_TOPIC = "0x" + keccak(b"Transfer(address,address,uint256)").hex()

IDENTITY_ABI = [
    {
        "inputs": [{"name": "agentId", "type": "uint256"}],
        "name": "ownerOf",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    }
]


class TrustTier(IntEnum):
    FULL = 0          # all checks pass + agent receipt valid
    CLIENT_ONLY = 1   # payment proven, response client-claimed (no receipt)
    DISPUTED = 2      # agent counter-attested a different interaction
    REJECTED = 3      # integrity or chain checks failed


def verify_integrity(content: bytes, feedback_hash: bytes) -> bool:
    """keccak256(content) == feedback_hash."""
    return keccak(content) == feedback_hash


def _topic_addr(topic: bytes) -> str:
    return to_checksum_address("0x" + topic.hex()[-40:])


def _parse_eip155_chain_id(chain_id: str) -> int:
    # settlement.chainId is stored as requirements.network, e.g. "eip155:8453"
    prefix, value = chain_id.split(":", 1)
    if prefix != "eip155":
        raise ValueError(f"unsupported chain id format: {chain_id}")
    return int(value)


def _canon_tx_hash(tx_hash: Any) -> str:
    if isinstance(tx_hash, (bytes, bytearray)):
        return "0x" + bytes(tx_hash).hex()
    s = str(tx_hash)
    if not s.startswith("0x"):
        s = "0x" + s
    return "0x" + s[2:].lower()


def _canon_addr(addr: Any) -> str:
    return to_checksum_address(str(addr))


def _agent_owner(w3: Any, identity_registry: str, agent_id: int) -> str:
    contract = w3.eth.contract(address=_canon_addr(identity_registry), abi=IDENTITY_ABI)
    owner = contract.functions.ownerOf(agent_id).call()
    return _canon_addr(owner)


def verify_settlement(w3: Any, artifact: dict[str, Any]) -> bool:
    """Confirm the settlement tx emitted a matching ERC-20 Transfer."""
    try:
        s = artifact["settlement"]
        expected_chain_id = _parse_eip155_chain_id(s["chainId"])
        if int(w3.eth.chain_id) != expected_chain_id:
            return False
        receipt = w3.eth.get_transaction_receipt(s["txHash"])
        if receipt.get("status") != 1:
            return False

        asset = _canon_addr(s["asset"])
        payer = _canon_addr(s["payer"])
        pay_to = _canon_addr(s["payTo"])
        amount = int(s["amount"])
        if amount <= 0:
            return False

        tx = w3.eth.get_transaction(s["txHash"])
        tx_to = _canon_addr(tx["to"])
        pm = str(s.get("paymentMethod", "")).lower()
        if tx_to != asset:
            # Exact Permit2 settlements call `x402ExactPermit2Proxy.settle`; the
            # ERC-20 `Transfer` still appears on the asset contract in receipt logs.
            if not (pm == "permit2" and tx_to == _canon_addr(X402_EXACT_PERMIT2_PROXY_ADDRESS)):
                return False

        reqs = s.get("paymentRequirements") or {}
        extra = reqs.get("extra") if isinstance(reqs, dict) else None
        if not isinstance(extra, dict):
            extra = {}
        allowed_from = {payer}
        if pm == "permit2":
            # Permit2 singleton; many Permit2 settlement paths emit `Transfer` with
            # `from` equal to the Permit2 contract rather than the payer EOA.
            allowed_from.add(_canon_addr("0x000000000022D473030F116dDEE9F6B43aC78BA3"))
        spender = extra.get("facilitatorAddress") or extra.get("spender")
        if spender:
            allowed_from.add(_canon_addr(str(spender)))
    except Exception:
        return False

    for log in receipt["logs"]:
        if _canon_addr(log["address"]) != asset:
            continue
        topics = log["topics"]
        if len(topics) != 3 or ("0x" + bytes(topics[0]).hex()) != TRANSFER_TOPIC:
            continue
        topic_from = _topic_addr(bytes(topics[1]))
        topic_to = _topic_addr(bytes(topics[2]))
        if topic_from not in allowed_from or topic_to != pay_to:
            continue
        data = bytes(log["data"])
        if len(data) != 32:
            continue
        if int(data.hex(), 16) == amount:
            return True
    return False


def verify_agent_binding(w3: Any, identity_registry: str, artifact: dict[str, Any]) -> bool:
    """ownerOf(agentId) must equal the settlement payTo."""
    try:
        agent_id = int(artifact["feedback"]["agentId"])
        pay_to = _canon_addr(artifact["settlement"]["payTo"])
        owner = _agent_owner(w3, identity_registry, agent_id)
        return owner == pay_to
    except Exception:
        return False


def verify_feedback(
    w3: Any,
    identity_registry: str,
    content: bytes,
    feedback_hash: bytes,
    artifact: dict[str, Any],
    *,
    submitter: str | None = None,
) -> TrustTier:
    """Full verification pipeline returning a trust tier."""
    try:
        if not verify_integrity(content, feedback_hash):
            return TrustTier.REJECTED
        if compute_feedback_hash(artifact) != feedback_hash:
            return TrustTier.REJECTED
        if not verify_settlement(w3, artifact):
            return TrustTier.REJECTED

        if submitter is not None and _canon_addr(submitter) != _canon_addr(artifact["settlement"]["payer"]):
            return TrustTier.REJECTED

        expected_chain_id = _parse_eip155_chain_id(artifact["settlement"]["chainId"])
        if int(w3.eth.chain_id) != expected_chain_id:
            return TrustTier.REJECTED

        agent_id = int(artifact["feedback"]["agentId"])
        owner = _agent_owner(w3, identity_registry, agent_id)
        if owner != _canon_addr(artifact["settlement"]["payTo"]):
            return TrustTier.REJECTED

        # If settlement.agentId is present, require it matches the rated agentId.
        if artifact["settlement"].get("agentId") is not None and int(artifact["settlement"]["agentId"]) != agent_id:
            return TrustTier.REJECTED
    except Exception:
        return TrustTier.REJECTED

    agent_sig = artifact["interaction"]["response"].get("agentSignature")
    if not agent_sig:
        return TrustTier.CLIENT_ONLY

    try:
        receipt = InteractionReceipt.from_dict(agent_sig)
    except Exception:
        return TrustTier.REJECTED

    try:
        if receipt.chain_id != expected_chain_id:
            return TrustTier.REJECTED
        if _canon_tx_hash(receipt.tx_hash) != _canon_tx_hash(artifact["settlement"]["txHash"]):
            return TrustTier.REJECTED
        if not verify_interaction_receipt(receipt, owner):
            return TrustTier.REJECTED
        if receipt.interaction_hash != compute_interaction_hash(artifact):
            return TrustTier.DISPUTED
        return TrustTier.FULL
    except Exception:
        return TrustTier.REJECTED


def dedup_feedback(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the latest (by block) record per (payer, agentId, txHash)."""
    best: dict[tuple, dict[str, Any]] = {}
    for r in records:
        payer = r.get("payer")
        agent_id = r.get("agentId")
        tx_hash = r.get("txHash")
        if payer is None or agent_id is None or tx_hash is None:
            continue
        key = (
            _canon_addr(payer),
            int(agent_id),
            _canon_tx_hash(tx_hash),
        )
        if key not in best or r["block"] > best[key]["block"]:
            best[key] = r
    return list(best.values())
