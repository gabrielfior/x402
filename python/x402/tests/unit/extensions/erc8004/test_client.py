"""Tests for ERC-8004 client extension."""

from x402.schemas.payments import PaymentPayload, PaymentRequired, PaymentRequirements

from x402.extensions.erc8004.client import (
    ERC8004ClientExtension,
    echo_erc8004_in_payment_payload,
    extract_erc8004_info,
)


def _make_requirements() -> PaymentRequirements:
    return PaymentRequirements(
        scheme="exact",
        network="eip155:1",
        asset="0x0000000000000000000000000000000000000000",
        amount="10000",
        pay_to="0x0000000000000000000000000000000000000000",
        max_timeout_seconds=60,
    )


def test_extract_erc8004_info() -> None:
    pr = PaymentRequired(
        accepts=[],
        extensions={"erc8004": {"info": {"agentId": 42}, "schema": {}}},
    )
    info = extract_erc8004_info(pr)
    assert info is not None
    assert info["agentId"] == 42


def test_extract_erc8004_info_missing() -> None:
    pr = PaymentRequired(accepts=[], extensions={})
    info = extract_erc8004_info(pr)
    assert info is None


def test_echo_erc8004_in_payment_payload() -> None:
    pr = PaymentRequired(
        accepts=[],
        extensions={"erc8004": {"info": {"agentId": 42}, "schema": {}}},
    )
    payload = PaymentPayload(payload={}, accepted=_make_requirements())
    result = echo_erc8004_in_payment_payload(payload, pr)
    assert result.extensions is not None
    assert "erc8004" in result.extensions
    assert result.extensions["erc8004"]["info"]["agentId"] == 42


def test_client_extension_key() -> None:
    ext = ERC8004ClientExtension()
    assert ext.key == "erc8004"


def test_client_extension_enrich() -> None:
    ext = ERC8004ClientExtension()
    pr = PaymentRequired(
        accepts=[],
        extensions={"erc8004": {"info": {"agentId": 42}, "schema": {}}},
    )
    payload = PaymentPayload(payload={}, accepted=_make_requirements())
    result = ext.enrich_payment_payload(payload, pr)
    assert result.extensions["erc8004"]["info"]["agentId"] == 42
