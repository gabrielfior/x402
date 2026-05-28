"""Example: Register ERC-8004 extension on an x402 resource server.

The extension declares the agentId in the 402 response. A meaningful agent
receipt must cover the response, which isn't known at settlement time, so it is
signed at the HTTP layer with `create_interaction_receipt(...)` and returned in
the `X-X402-Interaction-Receipt` header alongside the response body.
"""

from eth_account import Account
from x402 import x402ResourceServer
from x402.extensions.erc8004 import (
    create_erc8004_resource_server_extension,
    create_interaction_receipt,
    ERC8004Config,
)

# Agent owner key (must match IdentityRegistry.ownerOf(agent_id))
agent_owner = Account.from_key("0x...")

facilitator_client = ...  # your facilitator client

server = x402ResourceServer(facilitator_client)

config = ERC8004Config(
    network="eip155:8453",
    reputation_registry="0x8004BAa1...",
    identity_registry="0x...",
    rpc_url="https://...",
    agent_id=42,
)

server.register_extension(create_erc8004_resource_server_extension(config))


# After the resource handler runs and settlement completes, sign a receipt over
# the request + response and return it in a header. Pseudocode:
#
#   receipt = create_interaction_receipt(
#       agent_owner,
#       requirements=requirements,
#       payment_payload=payment_payload,
#       tx_hash=settle_result.transaction,
#       payer=settle_result.payer,
#       request={"method": "GET", "url": url,
#                "headerDigest": h_req, "bodyDigest": b_req},
#       response={"status": 200, "headerDigest": h_resp, "bodyDigest": b_resp},
#   )
#   response.headers["X-X402-Interaction-Receipt"] = json.dumps(receipt.to_dict())
