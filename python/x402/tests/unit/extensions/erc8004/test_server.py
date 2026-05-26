"""Tests for ERC-8004 server extension."""

from unittest.mock import MagicMock

from eth_account import Account
from x402.schemas.hooks import ServerPaymentRequiredContext, SettleResultContext
from x402.schemas.payments import PaymentPayload, PaymentRequirements
from x402.schemas.responses import SettleResponse

from x402.extensions.erc8004.server import create_erc8004_resource_server_extension
from x402.extensions.erc8004.types import ERC8004Config, InteractionReceipt
from x402.extensions.erc8004.artifact import verify_interaction_receipt


def _config(agent_id: int = 42) -> ERC8004Config:
    return ERC8004Config(
        network="eip155:8453",
        reputation_registry="0x" + "00" * 20,
        identity_registry="0x" + "00" * 20,
        rpc_url="http://localhost:8545",
        agent_id=agent_id,
    )


def _requirements() -> PaymentRequirements:
    return PaymentRequirements(
        scheme="exact",
        network="eip155:8453",
        asset="0x" + "01" * 20,
        amount="1000000",
        pay_to="0x" + "03" * 20,
        max_timeout_seconds=60,
    )


def test_extension_key() -> None:
    ext = create_erc8004_resource_server_extension(_config())
    assert ext.key == "erc8004"


def test_enrich_payment_required_response() -> None:
    ext = create_erc8004_resource_server_extension(_config())
    ctx = ServerPaymentRequiredContext(
        requirements=[], resource_info=None, error=None, payment_required_response=MagicMock()
    )
    result = ext.enrich_payment_required_response({}, ctx)
    assert result["info"]["agentId"] == 42
    assert "schema" in result


def test_no_receipt_without_signer() -> None:
    ext = create_erc8004_resource_server_extension(_config(), signer=None)
    ctx = SettleResultContext(
        payment_payload=PaymentPayload(payload={}, accepted=_requirements()),
        requirements=_requirements(),
        result=SettleResponse(success=True, transaction="0x" + "ab" * 32, network="eip155:8453", payer="0x" + "02" * 20),
    )
    assert ext.enrich_settlement_response({}, ctx) is None


def test_receipt_signed_and_verifiable() -> None:
    agent = Account.create()
    ext = create_erc8004_resource_server_extension(_config(), signer=agent)
    ctx = SettleResultContext(
        payment_payload=PaymentPayload(payload={"sig": "0xdead"}, accepted=_requirements()),
        requirements=_requirements(),
        result=SettleResponse(success=True, transaction="0x" + "ab" * 32, network="eip155:8453", payer="0x" + "02" * 20),
    )
    result = ext.enrich_settlement_response({}, ctx)
    assert "receipt" in result["info"]
    receipt = InteractionReceipt.from_dict(result["info"]["receipt"])
    assert receipt.chain_id == 8453
    assert verify_interaction_receipt(receipt, agent.address) is True
