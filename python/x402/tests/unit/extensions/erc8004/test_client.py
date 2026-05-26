"""Tests for ERC-8004 client extension."""

from unittest.mock import MagicMock

from x402.schemas.payments import PaymentPayload, PaymentRequired, PaymentRequirements

from x402.extensions.erc8004.client import (
    ERC8004ClientExtension,
    ERCFeedbackClient,
    InMemoryUploader,
    echo_erc8004_in_payment_payload,
    extract_erc8004_info,
)
from x402.extensions.erc8004.types import ERC8004Config, FeedbackParams


def _requirements() -> PaymentRequirements:
    return PaymentRequirements(
        scheme="exact",
        network="eip155:8453",
        asset="0x" + "01" * 20,
        amount="1000000",
        pay_to="0x" + "03" * 20,
        max_timeout_seconds=60,
    )


def _config() -> ERC8004Config:
    return ERC8004Config(
        network="eip155:8453",
        reputation_registry="0x" + "00" * 20,
        identity_registry="0x" + "00" * 20,
        rpc_url="http://localhost:8545",
    )


def test_extract_erc8004_info() -> None:
    pr = PaymentRequired(accepts=[], extensions={"erc8004": {"info": {"agentId": 42}, "schema": {}}})
    assert extract_erc8004_info(pr)["agentId"] == 42


def test_echo_erc8004_in_payment_payload() -> None:
    pr = PaymentRequired(accepts=[], extensions={"erc8004": {"info": {"agentId": 42}, "schema": {}}})
    payload = PaymentPayload(payload={}, accepted=_requirements())
    result = echo_erc8004_in_payment_payload(payload, pr)
    assert result.extensions["erc8004"]["info"]["agentId"] == 42


def test_client_extension_key() -> None:
    assert ERC8004ClientExtension().key == "erc8004"


def test_in_memory_uploader_returns_uri_and_keeps_bytes() -> None:
    up = InMemoryUploader()
    uri = up.upload(b'{"a":1}')
    assert uri.startswith("mem://")
    assert up.store[uri] == b'{"a":1}'


def test_pinata_uploader_posts_and_returns_ipfs_uri(monkeypatch) -> None:
    from x402.extensions.erc8004.client import PinataUploader

    captured = {}

    class _Resp:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return {"data": {"cid": "bafkreitestcid"}}

    def fake_post(url, headers=None, files=None, data=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["files"] = files
        captured["data"] = data
        return _Resp()

    import httpx

    monkeypatch.setattr(httpx, "post", fake_post)

    up = PinataUploader(jwt="TESTJWT")
    uri = up.upload(b'{"a":1}')

    assert uri == "ipfs://bafkreitestcid"
    assert up.last_cid == "bafkreitestcid"
    assert captured["url"] == "https://uploads.pinata.cloud/v3/files"
    assert captured["headers"]["Authorization"] == "Bearer TESTJWT"
    assert captured["data"]["network"] == "public"
    assert captured["files"]["file"][1] == b'{"a":1}'


def test_build_and_publish_sets_uri_and_hash() -> None:
    client = ERCFeedbackClient.__new__(ERCFeedbackClient)
    client._config = _config()
    up = InMemoryUploader()
    payload = PaymentPayload(payload={"sig": "0xdead"}, accepted=_requirements())
    params = FeedbackParams(agent_id=42, value=90, endpoint="/weather")
    out = client.build_and_publish_artifact(
        requirements=_requirements(),
        payment_payload=payload,
        tx_hash="0x" + "ab" * 32,
        payer="0x" + "02" * 20,
        payment_method="eip3009",
        request={"method": "GET", "url": "https://x/y", "headerDigest": "0x" + "00" * 32, "bodyDigest": "0x" + "00" * 32},
        response={"status": 200, "headerDigest": "0x" + "00" * 32, "bodyDigest": "0x" + "0a" * 32},
        params=params,
        uploader=up,
        receipt=None,
    )
    uri, feedback_hash, updated = out
    assert uri.startswith("mem://")
    assert len(feedback_hash) == 32
    assert updated.feedback_uri == uri
    assert updated.feedback_hash == feedback_hash
    # the bytes hosted at the URI hash to feedback_hash
    from eth_utils import keccak
    assert keccak(up.store[uri]) == feedback_hash


def test_submit_feedback_to_registry_builds_tx() -> None:
    client = ERCFeedbackClient.__new__(ERCFeedbackClient)
    client._config = _config()
    signer = MagicMock()
    signer.address = "0x" + "02" * 20
    client._signer = signer

    w3 = MagicMock()
    w3.eth.chain_id = 8453
    w3.eth.get_transaction_count.return_value = 7
    w3.eth.max_priority_fee = 1
    w3.eth.get_block.return_value = {"baseFeePerGas": 2}
    w3.eth.contract.return_value.functions.giveFeedback.return_value.build_transaction.return_value = {"data": "0xabcd"}
    signer.sign_transaction.return_value = MagicMock(raw_transaction=b"\x01")
    w3.eth.send_raw_transaction.return_value = bytes.fromhex("ab" * 32)
    client._w3 = w3

    params = FeedbackParams(agent_id=42, value=90, feedback_uri="mem://x", feedback_hash=b"\x0a" * 32)
    tx_hash = client.submit_feedback_to_registry(params)
    assert tx_hash == "0x" + "ab" * 32
    # plain type-2 tx (no EIP-7702 authorizationList)
    sent_tx = signer.sign_transaction.call_args[0][0]
    assert "authorizationList" not in sent_tx
