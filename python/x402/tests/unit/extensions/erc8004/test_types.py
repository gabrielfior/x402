"""Tests for ERC-8004 extension types."""

from x402.extensions.erc8004.types import (
    ERC8004Config,
    ERC8004ExtensionInfo,
    ERC8004ExtensionDeclaration,
    FeedbackParams,
    FeedbackTicket,
    EXTENSION_KEY,
)


def test_extension_key() -> None:
    assert EXTENSION_KEY == "erc8004"


def test_extension_info_model() -> None:
    info = ERC8004ExtensionInfo(agent_id=42)
    assert info.agent_id == 42


def test_extension_declaration() -> None:
    decl = ERC8004ExtensionDeclaration(
        info=ERC8004ExtensionInfo(agent_id=42),
        schema={"$schema": "https://json-schema.org/draft/2020-12/schema"},
    )
    assert decl.info.agent_id == 42
    assert "$schema" in decl.schema_


def test_feedback_params_defaults() -> None:
    params = FeedbackParams(agent_id=42, value=95)
    assert params.value_decimals == 0
    assert params.tag1 == ""
    assert params.feedback_hash == b"\x00" * 32


def test_feedback_ticket_roundtrip() -> None:
    ticket = FeedbackTicket(
        settlement_tx_hash=b"\x01" * 32,
        payer="0x1234567890123456789012345678901234567890",
        agent_id=42,
        nonce=1,
        signature=b"\x02" * 65,
    )
    d = ticket.to_dict()
    restored = FeedbackTicket.from_dict(d)
    assert restored.settlement_tx_hash == ticket.settlement_tx_hash
    assert restored.signature == ticket.signature
