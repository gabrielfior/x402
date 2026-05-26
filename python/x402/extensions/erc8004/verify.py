"""Verification and dedup for ERC-8004 x402 feedback.

Verification is payment-scheme agnostic: it keys off the universal ERC-20
Transfer event emitted by the asset contract, so EIP-3009, Permit2, and plain
ERC-20 settlements all verify the same way.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Any

from eth_utils import keccak, to_checksum_address

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


def verify_settlement(w3: Any, artifact: dict[str, Any]) -> bool:
    """Confirm the settlement tx emitted a matching ERC-20 Transfer."""
    s = artifact["settlement"]
    receipt = w3.eth.get_transaction_receipt(s["txHash"])
    asset = to_checksum_address(s["asset"])
    payer = to_checksum_address(s["payer"])
    pay_to = to_checksum_address(s["payTo"])
    amount = int(s["amount"])
    for log in receipt["logs"]:
        if to_checksum_address(log["address"]) != asset:
            continue
        topics = log["topics"]
        if len(topics) != 3 or ("0x" + bytes(topics[0]).hex()) != TRANSFER_TOPIC:
            continue
        if _topic_addr(bytes(topics[1])) != payer or _topic_addr(bytes(topics[2])) != pay_to:
            continue
        if int(bytes(log["data"]).hex() or "0", 16) == amount:
            return True
    return False


def verify_agent_binding(w3: Any, identity_registry: str, artifact: dict[str, Any]) -> bool:
    """ownerOf(agentId) must equal the settlement payTo."""
    agent_id = int(artifact["feedback"]["agentId"])
    pay_to = to_checksum_address(artifact["settlement"]["payTo"])
    contract = w3.eth.contract(
        address=to_checksum_address(identity_registry), abi=IDENTITY_ABI
    )
    owner = contract.functions.ownerOf(agent_id).call()
    return to_checksum_address(owner) == pay_to


def verify_feedback(
    w3: Any,
    identity_registry: str,
    content: bytes,
    feedback_hash: bytes,
    artifact: dict[str, Any],
) -> TrustTier:
    """Full verification pipeline returning a trust tier."""
    if not verify_integrity(content, feedback_hash):
        return TrustTier.REJECTED
    if compute_feedback_hash(artifact) != feedback_hash:
        return TrustTier.REJECTED
    if not verify_settlement(w3, artifact):
        return TrustTier.REJECTED
    if not verify_agent_binding(w3, identity_registry, artifact):
        return TrustTier.REJECTED

    agent_sig = artifact["interaction"]["response"].get("agentSignature")
    if not agent_sig:
        return TrustTier.CLIENT_ONLY

    receipt = InteractionReceipt.from_dict(agent_sig)
    owner = w3.eth.contract(
        address=to_checksum_address(identity_registry), abi=IDENTITY_ABI
    ).functions.ownerOf(int(artifact["feedback"]["agentId"])).call()
    if receipt.interaction_hash != compute_interaction_hash(artifact):
        return TrustTier.DISPUTED
    if not verify_interaction_receipt(receipt, owner):
        return TrustTier.DISPUTED
    return TrustTier.FULL


def dedup_feedback(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the latest (by block) record per (payer, agentId, txHash)."""
    best: dict[tuple, dict[str, Any]] = {}
    for r in records:
        payer = r["payer"]
        key = (
            to_checksum_address(payer) if str(payer).startswith("0x") and len(payer) == 42 else payer,
            r["agentId"],
            r["txHash"],
        )
        if key not in best or r["block"] > best[key]["block"]:
            best[key] = r
    return list(best.values())
