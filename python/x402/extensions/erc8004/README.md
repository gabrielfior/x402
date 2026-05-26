# ERC-8004 Feedback Extension

x402 v2 extension for submitting verified feedback to ERC-8004 ReputationRegistry.

## Architecture

- **No contracts owned.** Clients submit directly to the standard ERC-8004 `ReputationRegistry`.
- **Binding via off-chain artifact.** The client builds a canonical JSON artifact capturing `paymentRequirements`, `paymentPayload`, the response digest, and `txHash`, hashes it (keccak256) into `feedbackHash`, and uploads it to obtain `feedbackURI`.
- **Optional agent receipt.** The server may sign an `InteractionReceipt` over `{version, settlement, request, response}` (digests, not raw bodies) and return it in the `X-X402-Interaction-Receipt` header. It commits the agent to *what it served*; its absence downgrades the trust tier but never blocks submission.
- **Scheme-agnostic verification.** Verifiers key off the ERC-20 `Transfer` event, so EIP-3009 (USDC), Permit2, and plain ERC-20 all verify identically.
- **Dedup off-chain** on `(payer, agentId, settlementTxHash)`, latest block wins.

## Installation

```bash
pip install x402[evm]
```

## Server Usage

```python
import json
from x402 import x402ResourceServer
from x402.extensions.erc8004 import (
    create_erc8004_resource_server_extension, create_interaction_receipt, ERC8004Config,
)
from eth_account import Account

server = x402ResourceServer(facilitator_client)

config = ERC8004Config(
    network="eip155:8453",
    reputation_registry="0x8004BAa1...",
    identity_registry="0x...",
    rpc_url="https://...",
    agent_id=42,
)
server.register_extension(create_erc8004_resource_server_extension(config))

# After the handler runs and settlement completes, sign a receipt over the
# request + response (digests) and return it in a header:
agent_owner = Account.from_key("0x...")  # == IdentityRegistry.ownerOf(agentId)
receipt = create_interaction_receipt(
    agent_owner,
    requirements=requirements,
    payment_payload=payment_payload,
    tx_hash=settle_result.transaction,
    payer=settle_result.payer,
    request={"method": "GET", "url": url, "headerDigest": h_req, "bodyDigest": b_req},
    response={"status": 200, "headerDigest": h_resp, "bodyDigest": b_resp},
)
response.headers["X-X402-Interaction-Receipt"] = json.dumps(receipt.to_dict())
```

## Client Usage

```python
from x402.extensions.erc8004 import (
    ERCFeedbackClient, ERC8004Config, FeedbackParams, InMemoryUploader, InteractionReceipt,
)

config = ERC8004Config(
    network="eip155:8453",
    reputation_registry="0x8004BAa1...",
    identity_registry="0x...",
    rpc_url="https://...",
)
client = ERCFeedbackClient(config, signer)

# After payment: optionally parse the agent receipt from the settle response
receipt = InteractionReceipt.from_dict(receipt_dict) if receipt_dict else None

params = FeedbackParams(agent_id=42, value=95, tag1="x402", tag2="weather", endpoint="/weather")

uri, feedback_hash, params = client.build_and_publish_artifact(
    requirements=requirements,
    payment_payload=payment_payload,
    tx_hash=settle_tx_hash,
    payer=signer.address,
    payment_method="eip3009",       # or "permit2", "erc20"
    request={"method": "GET", "url": "...", "headerDigest": "0x..", "bodyDigest": "0x.."},
    response={"status": 200, "headerDigest": "0x..", "bodyDigest": "0x.."},
    params=params,
    uploader=InMemoryUploader(),    # production: an IPFS/Arweave uploader
    receipt=receipt,
)

tx_hash = client.submit_feedback_to_registry(params)
```

> When a receipt is present, the `settlement` block must byte-match what the server
> signed: pass the same `payment_method` the server derived
> (`requirements.extra["paymentMethod"]` if set, else `requirements.scheme`) and the
> same `payment_payload`/`requirements`. Otherwise `verify_feedback` returns
> `TrustTier.DISPUTED` instead of `FULL`.

## Verification (aggregators)

```python
from x402.extensions.erc8004 import verify_feedback, dedup_feedback, TrustTier

tier = verify_feedback(w3, config.identity_registry, artifact_bytes, feedback_hash, artifact_dict)
# TrustTier.FULL / CLIENT_ONLY / DISPUTED / REJECTED
```

## Future Work

- Response-digest coverage in the interaction receipt (v2 artifact schema)
- Content-addressed (IPFS/Arweave) uploader implementations
- Batch submission
