# ERC-8004 Feedback Extension

x402 v2 extension for submitting verified feedback to ERC-8004 ReputationRegistry.

## Architecture

- **Server**: Signs an off-chain EIP-712 ticket after settlement. No on-chain interaction.
- **Client**: Receives ticket in settlement response, submits feedback via single EIP-7702 type-4 tx.
- **Contract**: Verifies ticket by calling `IdentityRegistry.ownerOf(agentId)`, forwards to `ReputationRegistry`.

## Installation

```bash
pip install x402[evm]
```

## Server Usage

```python
from x402 import x402ResourceServer
from x402.extensions.erc8004 import create_erc8004_resource_server_extension, ERC8004Config
from eth_account import Account

server = x402ResourceServer(facilitator_client)

signer = Account.from_key("0x...")
config = ERC8004Config(
    network="eip155:1",
    feedback_gateway="0x...",
    reputation_registry="0x8004BAa1...",
    rpc_url="https://...",
    agent_id=42,
)

server.register_extension(create_erc8004_resource_server_extension(config, signer=signer))
```

## Client Usage

```python
from x402.extensions.erc8004 import ERCFeedbackClient, ERC8004Config, FeedbackParams

config = ERC8004Config(
    network="eip155:1",
    feedback_gateway="0x...",
    reputation_registry="0x8004BAa1...",
    rpc_url="https://...",
)

feedback_client = ERCFeedbackClient(config, signer)

# After payment, extract ticket from settle response
info = feedback_client.extract_erc8004_info(payment_required)
agent_id = info["agentId"]

# Build feedback
params = FeedbackParams(
    agent_id=agent_id,
    value=95,
    tag1="x402",
    tag2="weather",
    endpoint="https://example.com/weather",
)

# Submit via EIP-7702
tx_hash = feedback_client.submit_feedback(params, ticket)
```

## Gas Costs

- Single EIP-7702 type-4 tx: ~45k gas
- Mainnet @ 20 gwei: ~$2-5
- L2 (Base/Arbitrum): ~$0.01-0.10

## Future Work

- Facilitator-signed ticket fallback
- Unverified feedback path
- Batch submission
- L2 deployments
