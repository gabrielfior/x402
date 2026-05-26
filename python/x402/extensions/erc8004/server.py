"""Resource Server Extension for ERC-8004 feedback."""

from __future__ import annotations

from typing import Any

from x402.schemas.extensions import ResourceServerExtension
from x402.schemas.hooks import ServerPaymentRequiredContext, SettleResultContext

from .artifact import build_artifact, compute_interaction_hash, sign_interaction_receipt
from .schema import declare_erc8004_extension, erc8004_schema
from .types import ERC8004Config, EXTENSION_KEY


def create_erc8004_resource_server_extension(
    config: ERC8004Config,
    signer: Any | None = None,
) -> ResourceServerExtension:
    """Create ERC-8004 server extension.

    Declares agentId in PaymentRequired. On settlement, if a signer is provided,
    returns a signed InteractionReceipt attesting to the paid interaction. The
    receipt is optional: clients can still submit feedback without it.
    """
    agent_id = config.agent_id
    if agent_id is None:
        raise ValueError("agent_id is required in ERC8004Config for server extension")

    class ERC8004ResourceServerExtension:
        @property
        def key(self) -> str:
            return EXTENSION_KEY

        def enrich_declaration(self, declaration: Any, transport_context: Any) -> Any:
            return declaration

        def enrich_payment_required_response(
            self, declaration: Any, context: ServerPaymentRequiredContext
        ) -> dict[str, Any] | None:
            return declare_erc8004_extension(agent_id)

        def enrich_settlement_response(
            self, declaration: Any, context: SettleResultContext
        ) -> dict[str, Any] | None:
            if signer is None:
                return None
            result = context.result
            if not result.success or not result.transaction or not result.payer:
                return None

            requirements = context.requirements
            tx_hash = result.transaction
            chain_id = int(requirements.network.split(":")[1])

            artifact = build_artifact(
                requirements=requirements,
                payment_payload=context.payment_payload,
                tx_hash=tx_hash,
                payer=result.payer,
                payment_method=_payment_method(requirements),
                request={},
                response={},
                feedback={},
            )
            interaction_hash = compute_interaction_hash(artifact.to_dict())
            tx_hash_bytes = bytes.fromhex(tx_hash.removeprefix("0x"))
            receipt = sign_interaction_receipt(signer, chain_id, tx_hash_bytes, interaction_hash)

            return {"info": {"receipt": receipt.to_dict()}, "schema": erc8004_schema}

    return ERC8004ResourceServerExtension()


def _payment_method(requirements: Any) -> str:
    """Best-effort scheme tag for the artifact (informational only)."""
    extra = getattr(requirements, "extra", {}) or {}
    return extra.get("paymentMethod", requirements.scheme)
