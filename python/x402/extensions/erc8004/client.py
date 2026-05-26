"""Client-side utilities for the ERC-8004 Feedback Extension."""

from __future__ import annotations

import secrets
from typing import Any, Protocol

from eth_utils import to_checksum_address
from web3 import Web3
from x402.schemas.extensions import ClientExtension
from x402.schemas.payments import PaymentPayload, PaymentRequired, PaymentRequirements

from .artifact import build_artifact, canonical_bytes, compute_feedback_hash
from .schema import erc8004_schema
from .types import (
    ERC8004Config,
    EXTENSION_KEY,
    FeedbackParams,
    InteractionReceipt,
)

REPUTATION_ABI = [
    {
        "inputs": [
            {"name": "agentId", "type": "uint256"},
            {"name": "value", "type": "int128"},
            {"name": "valueDecimals", "type": "uint8"},
            {"name": "tag1", "type": "string"},
            {"name": "tag2", "type": "string"},
            {"name": "endpoint", "type": "string"},
            {"name": "feedbackURI", "type": "string"},
            {"name": "feedbackHash", "type": "bytes32"},
        ],
        "name": "giveFeedback",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]


def extract_erc8004_info(payment_required: PaymentRequired) -> dict[str, Any] | None:
    """Extract agentId from PaymentRequired.extensions."""
    if not payment_required.extensions:
        return None
    ext = payment_required.extensions.get(EXTENSION_KEY)
    if not ext:
        return None
    info = ext.get("info") if isinstance(ext, dict) else getattr(ext, "info", None)
    return info or None


def echo_erc8004_in_payment_payload(
    payment_payload: PaymentPayload, payment_required: PaymentRequired
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

    def enrich_payment_payload(self, payment_payload: Any, payment_required: Any) -> Any:
        return echo_erc8004_in_payment_payload(payment_payload, payment_required)


class ArtifactUploader(Protocol):
    """Pluggable storage backend for the feedback artifact.

    Production implementations should use content-addressed storage
    (IPFS/Arweave) so the URI itself commits to the content.
    """

    def upload(self, content: bytes) -> str:
        """Upload bytes, return a resolvable URI."""
        ...


class InMemoryUploader:
    """Test/dev uploader. Returns a mem:// URI and retains bytes in memory."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    def upload(self, content: bytes) -> str:
        uri = "mem://" + secrets.token_hex(16)
        self.store[uri] = content
        return uri


class PinataUploader:
    """Content-addressed uploader backed by the Pinata V3 file API.

    Posts to POST https://uploads.pinata.cloud/v3/files with Bearer auth.
    Returns an ipfs:// URI; the resulting CID is also kept on `last_cid`.
    """

    UPLOAD_URL = "https://uploads.pinata.cloud/v3/files"

    def __init__(
        self,
        jwt: str,
        network: str = "public",
        name: str = "x402-erc8004-feedback.json",
        timeout: float = 60.0,
    ) -> None:
        self._jwt = jwt
        self._network = network
        self._name = name
        self._timeout = timeout
        self.last_cid: str | None = None

    def upload(self, content: bytes) -> str:
        import httpx

        resp = httpx.post(
            self.UPLOAD_URL,
            headers={"Authorization": f"Bearer {self._jwt}"},
            files={"file": (self._name, content, "application/json")},
            data={"network": self._network, "name": self._name},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        cid = resp.json()["data"]["cid"]
        self.last_cid = cid
        return f"ipfs://{cid}"


class ERCFeedbackClient:
    """Client-side helper for building, publishing, and submitting feedback."""

    def __init__(self, config: ERC8004Config, signer: Any) -> None:
        self._config = config
        self._signer = signer
        self._w3 = Web3(Web3.HTTPProvider(config.rpc_url))

    @staticmethod
    def extract_erc8004_info(payment_required: PaymentRequired) -> dict[str, Any] | None:
        return extract_erc8004_info(payment_required)

    def build_and_publish_artifact(
        self,
        requirements: PaymentRequirements,
        payment_payload: PaymentPayload,
        tx_hash: str,
        payer: str,
        payment_method: str,
        request: dict[str, Any],
        response: dict[str, Any],
        params: FeedbackParams,
        uploader: ArtifactUploader,
        receipt: InteractionReceipt | None = None,
    ) -> tuple[str, bytes, FeedbackParams]:
        """Build the canonical artifact, embed the optional receipt, publish it.

        Returns (feedbackURI, feedbackHash, updated FeedbackParams).
        """
        feedback = {
            "agentId": params.agent_id,
            "value": params.value,
            "valueDecimals": params.value_decimals,
            "tag1": params.tag1,
            "tag2": params.tag2,
            "endpoint": params.endpoint,
            "comment": getattr(params, "comment", ""),
        }
        artifact = build_artifact(
            requirements=requirements,
            payment_payload=payment_payload,
            tx_hash=tx_hash,
            payer=payer,
            payment_method=payment_method,
            request=request,
            response=response,
            feedback=feedback,
        )
        art_dict = artifact.to_dict()
        if receipt is not None:
            art_dict["interaction"]["response"]["agentSignature"] = receipt.to_dict()

        feedback_hash = compute_feedback_hash(art_dict)
        uri = uploader.upload(canonical_bytes(art_dict))
        updated = params.model_copy(update={"feedback_uri": uri, "feedback_hash": feedback_hash})
        return uri, feedback_hash, updated

    def submit_feedback_to_registry(
        self, params: FeedbackParams, gas_limit: int = 250000
    ) -> str:
        """Submit feedback directly to ReputationRegistry.giveFeedback (type-2 tx)."""
        registry = self._w3.eth.contract(
            address=to_checksum_address(self._config.reputation_registry), abi=REPUTATION_ABI
        )
        func = registry.functions.giveFeedback(
            params.agent_id,
            params.value,
            params.value_decimals,
            params.tag1,
            params.tag2,
            params.endpoint,
            params.feedback_uri,
            params.feedback_hash,
        )

        sender = getattr(self._signer, "address", None)
        if sender is None:
            raise TypeError("signer must expose an address attribute")

        nonce = self._w3.eth.get_transaction_count(sender)
        base_fee = self._w3.eth.get_block("latest")["baseFeePerGas"]
        tx = {
            "type": 2,
            "chainId": self._w3.eth.chain_id,
            "nonce": nonce,
            "to": to_checksum_address(self._config.reputation_registry),
            "value": 0,
            "gas": gas_limit,
            "data": func.build_transaction({"from": sender})["data"],
            "maxFeePerGas": self._w3.eth.max_priority_fee + 2 * base_fee,
            "maxPriorityFeePerGas": self._w3.eth.max_priority_fee,
        }
        signed = self._signer.sign_transaction(tx)
        raw = self._w3.eth.send_raw_transaction(signed.raw_transaction)
        h = bytes(raw).hex()
        return h if h.startswith("0x") else "0x" + h
