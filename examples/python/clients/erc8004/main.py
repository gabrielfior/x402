"""Example: after paying for an x402 resource, submit verified ERC-8004 feedback.

These are the client-side steps that follow a successful x402 payment. The
`requirements`, `payment_payload`, and `settlement_tx_hash` come from the normal
x402 payment flow; the optional `receipt` is parsed from the server's
`X-X402-Interaction-Receipt` response header.

For a fully runnable end-to-end demo (local Anvil + real IPFS upload + on-chain
giveFeedback), run the integration test referenced in this directory's README.
"""

import json

from eth_account import Account
from x402.extensions.erc8004 import (
    ERC8004Config,
    ERCFeedbackClient,
    FeedbackParams,
    InteractionReceipt,
    PinataUploader,
    extract_erc8004_info,
)

signer = Account.from_key("0x...")  # funded client / payer EOA

config = ERC8004Config(
    network="eip155:8453",
    reputation_registry="0x8004BAa1...",
    identity_registry="0x...",
    rpc_url="https://...",
)
client = ERCFeedbackClient(config, signer)

# --- the following come from the standard x402 payment round-trip ---
# agent_id          = extract_erc8004_info(payment_required)["agentId"]
# requirements      = the accepted PaymentRequirements
# payment_payload   = your signed X-PAYMENT payload (EIP-3009 / Permit2 / ...)
# settlement_tx_hash= from the PAYMENT-RESPONSE header
# resp_body_digest  = keccak256 of the response body you received
# receipt_header    = response.headers.get("X-X402-Interaction-Receipt")
# receipt = InteractionReceipt.from_dict(json.loads(receipt_header)) if receipt_header else None

params = FeedbackParams(agent_id=agent_id, value=95, tag1="x402", tag2="weather", endpoint="/weather")

# 1) Build the canonical artifact and upload it to IPFS (real CID via Pinata).
uri, feedback_hash, params = client.build_and_publish_artifact(
    requirements=requirements,
    payment_payload=payment_payload,
    tx_hash=settlement_tx_hash,
    payer=signer.address,
    payment_method="eip3009",  # or "permit2", "erc20"
    request={"method": "GET", "url": "https://api.example.com/weather",
             "headerDigest": "0x" + "00" * 32, "bodyDigest": "0x" + "00" * 32},
    response={"status": 200, "headerDigest": "0x" + "00" * 32, "bodyDigest": resp_body_digest},
    params=params,
    uploader=PinataUploader(jwt="<PINATA_JWT>"),
    receipt=receipt,
)
print(f"artifact: {uri}")

# 2) Submit feedback directly to the standard ReputationRegistry (type-2 tx).
tx_hash = client.submit_feedback_to_registry(params)
print(f"feedback submitted on-chain: {tx_hash}")
