"""Example: Pay for a resource and submit verified feedback."""

from eth_account import Account
from x402.extensions.erc8004 import (
    ERC8004Config,
    ERCFeedbackClient,
    FeedbackParams,
    extract_erc8004_info,
)

client_key = "0x..."
signer = Account.from_key(client_key)

config = ERC8004Config(
    network="eip155:1",
    feedback_gateway="0x...",
    reputation_registry="0x8004BAa1...",
    rpc_url="https://...",
)

feedback_client = ERCFeedbackClient(config, signer)

# After receiving 402 and paying
agent_id = extract_erc8004_info(payment_required)["agentId"]

# Extract ticket from settlement response
ticket_data = settle_response.extensions["erc8004"]["info"]["ticket"]

from x402.extensions.erc8004.types import FeedbackTicket
ticket = FeedbackTicket.from_dict(ticket_data)

# Build feedback params
params = FeedbackParams(
    agent_id=agent_id,
    value=95,
    tag1="x402",
    tag2="weather",
    endpoint="https://api.example.com/weather",
)

# Submit via EIP-7702
tx_hash = feedback_client.submit_feedback(params, ticket)
print(f"Feedback submitted: {tx_hash}")
