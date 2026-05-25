"""Tests for ERC-8004 server extension."""

from unittest.mock import MagicMock

import pytest
from eth_account import Account
from x402.schemas.hooks import ServerPaymentRequiredContext, SettleResultContext
from x402.schemas.payments import PaymentRequirements
from x402.schemas.responses import SettleResponse

from x402.extensions.erc8004.server import create_erc8004_resource_server_extension
from x402.extensions.erc8004.types import ERC8004Config


def test_extension_key() -> None:
    config = ERC8004Config(
        network="eip155:1",
        feedback_gateway="0x0000000000000000000000000000000000000000",
        reputation_registry="0x0000000000000000000000000000000000000000",
        rpc_url="http://localhost:8545",
        agent_id=42,
    )
    ext = create_erc8004_resource_server_extension(config)
    assert ext.key == "erc8004"


def test_enrich_payment_required_response() -> None:
    config = ERC8004Config(
        network="eip155:1",
        feedback_gateway="0x0000000000000000000000000000000000000000",
        reputation_registry="0x0000000000000000000000000000000000000000",
        rpc_url="http://localhost:8545",
        agent_id=42,
    )
    ext = create_erc8004_resource_server_extension(config)
    ctx = ServerPaymentRequiredContext(
        requirements=[],
        resource_info=None,
        error=None,
        payment_required_response=MagicMock(),
    )
    result = ext.enrich_payment_required_response({}, ctx)
    assert result is not None
    assert result["info"]["agentId"] == 42
    assert "schema" in result


def test_enrich_settlement_response_no_signer() -> None:
    config = ERC8004Config(
        network="eip155:1",
        feedback_gateway="0x0000000000000000000000000000000000000000",
        reputation_registry="0x0000000000000000000000000000000000000000",
        rpc_url="http://localhost:8545",
        agent_id=42,
    )
    ext = create_erc8004_resource_server_extension(config, signer=None)
    settle_result = SettleResponse(
        success=True,
        transaction="0x1234567890abcdef" * 2,
        network="eip155:1",
        payer="0x1234567890123456789012345678901234567890",
    )
    ctx = SettleResultContext(
        payment_payload=MagicMock(),
        requirements=PaymentRequirements(
            scheme="exact",
            network="eip155:1",
            asset="0x0000000000000000000000000000000000000000",
            amount="10000",
            pay_to="0x0000000000000000000000000000000000000000",
            max_timeout_seconds=60,
        ),
        result=settle_result,
    )
    result = ext.enrich_settlement_response({}, ctx)
    assert result is None


def test_enrich_settlement_response_with_signer() -> None:
    signer = Account.create()
    config = ERC8004Config(
        network="eip155:1",
        feedback_gateway="0x0000000000000000000000000000000000000000",
        reputation_registry="0x0000000000000000000000000000000000000000",
        rpc_url="http://localhost:8545",
        agent_id=42,
    )
    ext = create_erc8004_resource_server_extension(config, signer=signer)
    settle_result = SettleResponse(
        success=True,
        transaction="0x1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef",
        network="eip155:1",
        payer="0x1234567890123456789012345678901234567890",
    )
    ctx = SettleResultContext(
        payment_payload=MagicMock(),
        requirements=PaymentRequirements(
            scheme="exact",
            network="eip155:1",
            asset="0x0000000000000000000000000000000000000000",
            amount="10000",
            pay_to="0x0000000000000000000000000000000000000000",
            max_timeout_seconds=60,
        ),
        result=settle_result,
    )
    result = ext.enrich_settlement_response({}, ctx)
    assert result is not None
    assert "ticket" in result["info"]
    assert result["info"]["ticket"]["agentId"] == 42
