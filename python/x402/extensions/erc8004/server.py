"""Resource Server Extension for ERC-8004 feedback."""

from __future__ import annotations

import secrets
from typing import Any

from eth_account import Account
from eth_utils import keccak, to_checksum_address
from x402.schemas.extensions import ResourceServerExtension
from x402.schemas.hooks import ServerPaymentRequiredContext, SettleResultContext

from .schema import declare_erc8004_extension, erc8004_schema
from .types import ERC8004Config, EXTENSION_KEY, FeedbackTicket


def create_erc8004_resource_server_extension(
    config: ERC8004Config,
    signer: Any | None = None,
) -> ResourceServerExtension:
    """Create ERC-8004 server extension.

    Enriches PaymentRequired with extensions["erc8004"].agentId.
    Signs an EIP-712 ticket in enrich_settlement_response.

    Args:
        config: Chain and contract addresses.
        signer: Anything with a `.sign_message(message_hash)` method or an
            eth_account LocalAccount. If None, no tickets are signed.
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
            self,
            declaration: Any,
            context: ServerPaymentRequiredContext,
        ) -> dict[str, Any] | None:
            return declare_erc8004_extension(agent_id)

        def enrich_settlement_response(
            self,
            declaration: Any,
            context: SettleResultContext,
        ) -> dict[str, Any] | None:
            if signer is None:
                return None

            result = context.result
            if not result.success:
                return None

            tx_hash_str = result.transaction
            if not tx_hash_str:
                return None

            payer = result.payer
            if not payer:
                return None

            tx_hash = bytes.fromhex(tx_hash_str.removeprefix("0x"))
            payer = to_checksum_address(payer)
            nonce = secrets.randbits(256)

            digest = keccak(
                b"\x19\x01"
                + _encode_chain_id(context.requirements.network)
                + tx_hash
                + bytes.fromhex(payer.removeprefix("0x"))
                + _encode_uint256(agent_id)
                + _encode_uint256(nonce)
            )

            if hasattr(signer, "sign_message"):
                from eth_account.messages import encode_defunct
                sig = signer.sign_message(encode_defunct(digest))
            elif hasattr(signer, "signHash"):
                sig = signer.signHash(digest)
            else:
                raise TypeError(
                    "signer must have sign_message or signHash method"
                )

            ticket = FeedbackTicket(
                settlement_tx_hash=tx_hash,
                payer=payer,
                agent_id=agent_id,
                nonce=nonce,
                signature=sig.signature if hasattr(sig, "signature") else sig,
            )

            return {
                "info": {"ticket": ticket.to_dict()},
                "schema": erc8004_schema,
            }

    return ERC8004ResourceServerExtension()


def _encode_chain_id(network: str) -> bytes:
    parts = network.split(":")
    if len(parts) != 2:
        raise ValueError(f"invalid network format: {network}")
    chain_id = int(parts[1])
    return chain_id.to_bytes(32, "big")


def _encode_uint256(value: int) -> bytes:
    return value.to_bytes(32, "big")
