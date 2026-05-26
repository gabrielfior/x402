"""Resource Server Extension for ERC-8004 feedback."""

from __future__ import annotations

from typing import Any

from x402.schemas.extensions import ResourceServerExtension
from x402.schemas.hooks import ServerPaymentRequiredContext, SettleResultContext
from x402.schemas.payments import PaymentPayload, PaymentRequirements

from .artifact import build_artifact, compute_interaction_hash, sign_interaction_receipt
from .schema import declare_erc8004_extension
from .types import ERC8004Config, EXTENSION_KEY, InteractionReceipt


def create_erc8004_resource_server_extension(
    config: ERC8004Config,
) -> ResourceServerExtension:
    """Create ERC-8004 server extension.

    Declares agentId in the 402 response. It does NOT sign anything at settlement
    time: a meaningful agent receipt must cover the response, which is not known
    in the settle hook. Sign the receipt at the HTTP layer with
    `create_interaction_receipt(...)` once the response digest is available, and
    return it in the `X-X402-Interaction-Receipt` header.
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
            # The receipt covers the response, which isn't in the settle context.
            # It is produced at the HTTP layer instead (see create_interaction_receipt).
            return None

    return ERC8004ResourceServerExtension()


def create_interaction_receipt(
    signer: Any,
    *,
    requirements: PaymentRequirements,
    payment_payload: PaymentPayload,
    tx_hash: str,
    payer: str,
    request: dict[str, Any],
    response: dict[str, Any],
    payment_method: str | None = None,
) -> InteractionReceipt:
    """Sign an InteractionReceipt over {version, settlement, request, response}.

    Call this at the HTTP layer, after the resource handler runs, once the
    response digests are known. `request`/`response` carry digests (not raw
    bodies). The returned receipt is meant for the `X-X402-Interaction-Receipt`
    response header; the client embeds it at interaction.response.agentSignature.

    The signer must be the agent owner key (IdentityRegistry.ownerOf(agentId)).
    """
    pm = payment_method or _payment_method(requirements)
    artifact = build_artifact(
        requirements=requirements,
        payment_payload=payment_payload,
        tx_hash=tx_hash,
        payer=payer,
        payment_method=pm,
        request=request,
        response=response,
        feedback={},
    )
    interaction_hash = compute_interaction_hash(artifact.to_dict())
    chain_id = int(requirements.network.split(":")[1])
    tx_bytes = bytes.fromhex(tx_hash.removeprefix("0x"))
    return sign_interaction_receipt(signer, chain_id, tx_bytes, interaction_hash)


def _payment_method(requirements: Any) -> str:
    """Best-effort scheme tag for the artifact (informational only)."""
    extra = getattr(requirements, "extra", {}) or {}
    return extra.get("paymentMethod", requirements.scheme)
