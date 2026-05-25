"""Example: Register ERC-8004 extension on an x402 resource server."""

from eth_account import Account
from x402 import x402ResourceServer
from x402.extensions.erc8004 import create_erc8004_resource_server_extension, ERC8004Config

# Load server signing key (must match IdentityRegistry.ownerOf(agent_id))
server_key = "0x..."
signer = Account.from_key(server_key)

facilitator_client = ...  # your facilitator client

server = x402ResourceServer(facilitator_client)

config = ERC8004Config(
    network="eip155:1",
    feedback_gateway="0x...",
    reputation_registry="0x8004BAa1...",
    rpc_url="https://...",
    agent_id=42,
)

server.register_extension(
    create_erc8004_resource_server_extension(config, signer=signer)
)
