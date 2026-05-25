"""Client-side utilities for the ERC-8004 Feedback Extension."""

from __future__ import annotations

from typing import Any

from eth_account import Account
from eth_utils import keccak, to_checksum_address
from web3 import Web3
from x402.schemas.extensions import ClientExtension
from x402.schemas.payments import PaymentPayload, PaymentRequired

from .schema import erc8004_schema
from .types import ERC8004Config, EXTENSION_KEY, FeedbackParams, FeedbackTicket


def extract_erc8004_info(payment_required: PaymentRequired) -> dict[str, Any] | None:
    """Extract agentId from PaymentRequired.extensions."""
    if not payment_required.extensions:
        return None
    ext = payment_required.extensions.get(EXTENSION_KEY)
    if not ext:
        return None
    info = ext.get("info") if isinstance(ext, dict) else getattr(ext, "info", None)
    if not info:
        return None
    return info


def echo_erc8004_in_payment_payload(
    payment_payload: PaymentPayload,
    payment_required: PaymentRequired,
) -> PaymentPayload:
    """Echo the erc8004 extension into PaymentPayload per x402 v2 spec."""
    if not payment_required.extensions or EXTENSION_KEY not in payment_required.extensions:
        return payment_payload

    ext = payment_required.extensions[EXTENSION_KEY]
    info = ext.get("info") if isinstance(ext, dict) else getattr(ext, "info", {})

    extensions = dict(payment_payload.extensions or {})
    extensions[EXTENSION_KEY] = {"info": dict(info), "schema": erc8004_schema}
    payment_payload.extensions = extensions
    return payment_payload


class ERC8004ClientExtension(ClientExtension):
    """Client extension that echoes erc8004 info into PaymentPayload."""

    key = EXTENSION_KEY

    def enrich_payment_payload(
        self,
        payment_payload: Any,
        payment_required: Any,
    ) -> Any:
        return echo_erc8004_in_payment_payload(payment_payload, payment_required)


class ERCFeedbackClient:
    """Client-side helper for submitting verified feedback."""

    def __init__(self, config: ERC8004Config, signer: Any) -> None:
        self._config = config
        self._signer = signer
        self._w3 = Web3(Web3.HTTPProvider(config.rpc_url))

    @staticmethod
    def extract_erc8004_info(payment_required: PaymentRequired) -> dict[str, Any] | None:
        return extract_erc8004_info(payment_required)

    def check_duplicate(self, nonce: int) -> bool:
        """Query FeedbackGateway.usedNonces(nonce)."""
        gateway = self._w3.eth.contract(
            address=to_checksum_address(self._config.feedback_gateway),
            abi=[
                {
                    "inputs": [{"name": "", "type": "uint256"}],
                    "name": "usedNonces",
                    "outputs": [{"name": "", "type": "bool"}],
                    "stateMutability": "view",
                    "type": "function",
                }
            ],
        )
        return gateway.functions.usedNonces(nonce).call()

    def submit_feedback(
        self,
        params: FeedbackParams,
        ticket: FeedbackTicket,
        gas_limit: int = 200000,
    ) -> str:
        """Build and send EIP-7702 type-4 tx delegating to FeedbackGateway.

        Returns the transaction hash hex string.
        """
        gateway_addr = to_checksum_address(self._config.feedback_gateway)
        registry_addr = to_checksum_address(self._config.reputation_registry)

        gateway = self._w3.eth.contract(
            address=gateway_addr,
            abi=[
                {
                    "inputs": [
                        {"name": "registry", "type": "address"},
                        {
                            "name": "params",
                            "type": "tuple",
                            "components": [
                                {"name": "agentId", "type": "uint256"},
                                {"name": "value", "type": "int128"},
                                {"name": "valueDecimals", "type": "uint8"},
                                {"name": "tag1", "type": "string"},
                                {"name": "tag2", "type": "string"},
                                {"name": "endpoint", "type": "string"},
                                {"name": "feedbackURI", "type": "string"},
                                {"name": "feedbackHash", "type": "bytes32"},
                            ],
                        },
                        {
                            "name": "ticket",
                            "type": "tuple",
                            "components": [
                                {"name": "settlementTxHash", "type": "bytes32"},
                                {"name": "payer", "type": "address"},
                                {"name": "agentId", "type": "uint256"},
                                {"name": "nonce", "type": "uint256"},
                                {"name": "signature", "type": "bytes"},
                            ],
                        },
                    ],
                    "name": "submitFeedback",
                    "outputs": [],
                    "stateMutability": "nonpayable",
                    "type": "function",
                }
            ],
        )

        func_call = gateway.functions.submitFeedback(
            registry_addr,
            (
                params.agent_id,
                params.value,
                params.value_decimals,
                params.tag1,
                params.tag2,
                params.endpoint,
                params.feedback_uri,
                params.feedback_hash,
            ),
            (
                ticket.settlement_tx_hash,
                ticket.payer,
                ticket.agent_id,
                ticket.nonce,
                ticket.signature,
            ),
        )

        if hasattr(self._signer, "address"):
            sender = self._signer.address
        elif hasattr(self._signer, "_address"):
            sender = self._signer._address
        else:
            raise TypeError("signer must expose an address attribute")

        tx_nonce = self._w3.eth.get_transaction_count(sender)

        auth = Account.sign_authorization(
            {
                "chainId": self._w3.eth.chain_id,
                "address": gateway_addr,
                "nonce": tx_nonce + 1,
            },
            self._signer.key if hasattr(self._signer, "key") else self._signer,
        )

        tx = {
            "type": 4,
            "chainId": self._w3.eth.chain_id,
            "nonce": tx_nonce,
            "to": sender,
            "value": 0,
            "gas": gas_limit,
            "data": func_call.build_transaction({"from": sender})["data"],
            "authorizationList": [auth],
        }

        max_fee = self._w3.eth.max_priority_fee + (2 * self._w3.eth.get_block("latest")["baseFeePerGas"])
        tx["maxFeePerGas"] = max_fee
        tx["maxPriorityFeePerGas"] = self._w3.eth.max_priority_fee

        signed = self._signer.sign_transaction(tx)
        tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction)
        return tx_hash.hex()
