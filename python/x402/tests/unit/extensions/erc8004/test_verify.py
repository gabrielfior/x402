"""Tests for ERC-8004 verification + dedup."""

from eth_utils import keccak

from x402.extensions.erc8004.verify import (
    TrustTier,
    verify_integrity,
    dedup_feedback,
)


def test_verify_integrity_match() -> None:
    content = b'{"a":1}'
    assert verify_integrity(content, keccak(content)) is True


def test_verify_integrity_mismatch() -> None:
    assert verify_integrity(b'{"a":1}', b"\x00" * 32) is False


def test_dedup_keeps_latest_per_key() -> None:
    records = [
        {"payer": "0xA", "agentId": 1, "txHash": "0xT", "block": 10, "value": 50},
        {"payer": "0xA", "agentId": 1, "txHash": "0xT", "block": 20, "value": 90},
        {"payer": "0xB", "agentId": 1, "txHash": "0xT", "block": 5, "value": 30},
    ]
    out = dedup_feedback(records)
    assert len(out) == 2
    a = [r for r in out if r["payer"] == "0xA"][0]
    assert a["value"] == 90  # latest block wins


def test_trust_tier_values() -> None:
    assert {t.name for t in TrustTier} >= {"FULL", "CLIENT_ONLY", "DISPUTED", "REJECTED"}
