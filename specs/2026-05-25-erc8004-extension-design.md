# ERC-8004 Feedback Extension for x402 — Design Specification

**Date:** 2026-05-25  
**Status:** Design approved, pending implementation  
**Scope:** Python SDK extension only (TypeScript and Go deferred to follow-up PRs)

---

## 1. Purpose

Enable x402-paying clients to submit verified feedback to ERC-8004 `ReputationRegistry` after successful payment. The extension bridges the x402 payment protocol with the ERC-8004 agent reputation system using EIP-7702 delegation for correct `msg.sender` attribution.

## 2. Terminology

- **ERC-8004** — Ethereum standard for decentralized AI agent reputation (registries at `0x8004...`)
- **ReputationRegistry** — Stores feedback given to agents (`giveFeedback`)
- **IdentityRegistry** — Registers agents and assigns `agentId`
- **FeedbackGateway** — EIP-7702 delegate contract that verifies settlement + dedups + forwards feedback to ReputationRegistry
- **EIP-7702** — Pectra hardfork feature allowing EOAs to delegate code execution while preserving `msg.sender`

## 3. Design Principles

1. **No x402 core changes** — Uses existing v2 `extensions` field only
2. **Fork-friendly** — Single directory, clear separation, all addresses configurable
3. **Transport-agnostic** — Works for HTTP, MCP, WebSocket, etc.
4. **Graceful degradation** — Missing extension → unverified feedback still possible

## 4. Architecture

### 4.1 Server Side

The resource server advertises its ERC-8004 `agentId` in the `PaymentRequired.extensions` field. After settlement, it records the settlement txHash on the chain-wide `FeedbackGateway` contract so the client can verify who paid.

```python
# Server enriches PaymentRequired
PaymentRequired(
    x402Version=2,
    accepts=[...],
    extensions={
        "erc8004": {
            "agentId": 42  # Server's IdentityRegistry tokenId
        }
    }
)
```

### 4.2 Client Side

The client:
1. Extracts `agentId` from `PaymentRequired.extensions["erc8004"]`
2. Pays via standard x402 flow
3. Polls `FeedbackGateway.settlementPayer(txHash)` to verify payer
4. Builds EIP-7702 type-4 tx delegating to `FeedbackGateway`
5. Calls `submitFeedback()` which dedups, verifies settlement, and forwards to `ReputationRegistry`

### 4.3 Data Flow

```
Client requests resource
    ↓
Server returns 402 PaymentRequired + extensions["erc8004"].agentId
    ↓
Client pays → Facilitator settles on-chain (or server settles directly)
    ↓
Server on_after_settle hook: FeedbackGateway.recordSettlement(txHash, payer)
    ↓
Server returns paid response
    ↓
Client polls FeedbackGateway.settlementPayer(txHash) → verifies payer == self
    ↓
Client signs EIP-7702 type-4 tx: EOA delegates to FeedbackGateway
    ↓
FeedbackGateway.submitFeedback(agentId, params, settlementTxHash)
    ↓
Dedup check + settlement verification → IReputationRegistry.giveFeedback()
    ↓
Feedback attributed to client EOA (msg.sender = EOA thanks to EIP-7702)
```

## 5. Components

### 5.1 Extension Package Structure

```
python/x402/extensions/erc8004/
├── __init__.py          # Public API exports
├── types.py             # FeedbackParams, ERC8004Config, ERC8004ExtensionDeclaration
├── constants.py         # Mainnet registry addresses, network configs
├── server.py            # ResourceServerExtension (enrich + on_after_settle)
├── client.py            # ERCFeedbackClient (extract + verify + submit)
└── README.md            # Usage guide

contracts/erc8004/
├── FeedbackGateway.sol         # EIP-7702 delegate + settlement verification
├── interfaces/
│   └── IReputationRegistry.sol # Minimal interface
└── foundry.toml                # Build config

examples/python/clients/erc8004/
├── main.py              # Full demo: pay + submit feedback
├── server_example.py    # How to register server extension
├── pyproject.toml       # Dependencies
└── README.md            # Setup instructions
```

### 5.2 Types (`types.py`)

```python
@dataclass
class FeedbackParams:
    """Parameters for ReputationRegistry.giveFeedback"""
    agent_id: int
    value: int           # Feedback score (e.g., 0-100)
    value_decimals: int  # Usually 0
    tag1: str            # Service category (e.g., "weather")
    tag2: str            # Sub-category (e.g., "current")
    endpoint: str        # Full URL of the resource
    feedback_uri: str    # Optional URI for detailed feedback
    feedback_hash: bytes  # 32 bytes, keccak256(agentId, settlementTxHash)

@dataclass
class ERC8004Config:
    """Chain-specific config"""
    network: str                  # CAIP-2 network ID (e.g., "eip155:1")
    feedback_gateway: str         # Chain-wide FeedbackGateway address
    reputation_registry: str      # ReputationRegistry address
    identity_registry: str        # IdentityRegistry address
    rpc_url: str                  # Web3 RPC endpoint
    agent_id: int | None = None   # Server-only: this server's agentId

@dataclass
class ERC8004ExtensionDeclaration:
    """What server puts in PaymentRequired.extensions['erc8004']"""
    agent_id: int
```

### 5.3 Server Extension (`server.py`)

```python
def create_erc8004_resource_server_extension(
    config: ERC8004Config
) -> ResourceServerExtension:
    """Create ERC-8004 server extension.

    - Enriches PaymentRequired with extensions["erc8004"].agentId
    - Records settlement on FeedbackGateway after successful settle
    """
```

**Hook implementations:**
- `enrich_payment_required_response(declaration, context)` → returns `{"agentId": config.agent_id}`
- `on_after_settle(declaration, context)` → calls `FeedbackGateway.recordSettlement(txHash, payer)`

**Error handling:** `recordSettlement` failure is logged as warning, never blocks the settlement response.

### 5.4 Client Extension (`client.py`)

```python
class ERCFeedbackClient:
    """Client-side helper for submitting verified feedback."""

    def __init__(self, config: ERC8004Config, signer: EthAccountSigner):
        ...

    @staticmethod
    def extract_erc8004_info(
        payment_required: PaymentRequired
    ) -> ERC8004ExtensionDeclaration | None:
        """Extract agentId from PaymentRequired.extensions"""
        ...

    def verify_settlement(self, tx_hash: bytes) -> str | None:
        """Poll FeedbackGateway.settlementPayer(txHash).
        Returns payer address if recorded, None otherwise.
        """
        ...

    def check_duplicate(self, feedback_hash: bytes) -> bool:
        """Query FeedbackGateway.hasBeenUsed(feedbackHash)"""
        ...

    def submit_feedback(
        self,
        params: FeedbackParams,
        settlement_tx_hash: bytes32,
    ) -> str:
        """Build and send EIP-7702 type-4 tx.

        - Signs authorization delegating EOA to FeedbackGateway
        - Calls submitFeedback() via type-4 transaction
        - Returns transaction hash
        """
        ...
```

### 5.5 Contracts

**FeedbackGateway.sol** — Moved from naive implementation. Key functions:
- `recordSettlement(bytes32 txHash, address payer)` — permissionless, first-writer wins
- `settlementPayer(bytes32 txHash)` → address — client polls this
- `hasBeenUsed(bytes32 hash)` → bool — dedup check
- `submitFeedback(address registry, FeedbackParams params, bytes32 settlementTxHash)` — EIP-7702 entrypoint
- `tryClaimSettlement(bytes32 txHash, address caller)` — internal, called via external call to dedupStore

**IReputationRegistry.sol** — Minimal interface:
```solidity
interface IReputationRegistry {
    function giveFeedback(
        uint256 agentId,
        int128 value,
        uint8 valueDecimals,
        string calldata tag1,
        string calldata tag2,
        string calldata endpoint,
        string calldata feedbackURI,
        bytes32 feedbackHash
    ) external;
}
```

## 6. Gas Cost Considerations

Submitting feedback requires an EIP-7702 type-4 transaction, which costs gas. On Ethereum mainnet this could range from $5–50+ depending on network congestion.

**Future work (documented but not implemented in v1):**
Several options exist for removing this gas burden from clients:
- **Server reimbursement:** Server sends a small ETH amount to the client address after settlement, which the client uses for the feedback tx
- **Batch submission:** Client accumulates multiple feedback intents and submits them in a single EIP-7702 tx
- **L2 deployment:** Deploy FeedbackGateway and ReputationRegistry on an L2 (Base, Arbitrum) where gas costs are ~$0.01–0.10
- **Meta-transactions / paymasters:** Use EIP-4337 paymasters to sponsor EIP-7702 tx gas (requires infrastructure maturity)

For the initial PR, gas costs are documented in the README. Production deployments should consider the above strategies.

## 7. Testing Plan

### 7.1 Unit Tests
- Extension declaration enrichment (`server.py`)
- Settlement recording hook (`server.py`)
- Client: extract agentId from PaymentRequired (`client.py`)
- Client: verify settlement polling (`client.py`)
- Client: EIP-7702 tx building (without sending) (`client.py`)
- Duplicate detection logic (`client.py`)

### 7.2 Integration Test
- Full flow against local Anvil (Prague hardfork)
- Fork mainnet or use blank Anvil with contract deployment
- Assert:
  - `FeedbackGateway.settlementPayer(txHash) == client_address`
  - `ReputationRegistry.getSummary(agentId)` has feedback count > 0
  - `ReputationRegistry.getLastIndex(agentId, client_address)` > 0
  - Feedback value matches submitted value

### 7.3 Mock Testing
- Mock `FeedbackGateway` contract for unit tests (no RPC needed)
- Mock `SettleResponse` for testing server hook

## 8. Example Usage

### 8.1 Server Registration

```python
from x402 import x402ResourceServer
from x402.extensions.erc8004 import create_erc8004_resource_server_extension, ERC8004Config

server = x402ResourceServer(facilitator_client)

config = ERC8004Config(
    network="eip155:1",
    feedback_gateway="0x...",
    reputation_registry="0x8004BAa1...",
    identity_registry="0x8004A169...",
    rpc_url="https://...",
    agent_id=42,
)

server.register_extension(create_erc8004_resource_server_extension(config))
```

### 8.2 Client Usage

```python
from x402.extensions.erc8004 import ERCFeedbackClient, ERC8004Config, FeedbackParams

config = ERC8004Config(
    network="eip155:1",
    feedback_gateway="0x...",
    reputation_registry="0x8004BAa1...",
    identity_registry="0x8004A169...",
    rpc_url="https://...",
)

feedback_client = ERCFeedbackClient(config, signer)

# After receiving 402 PaymentRequired
agent_id = feedback_client.extract_erc8004_info(payment_required).agent_id

# After paying and receiving settlement txHash
settlement_tx = bytes32(...)  # from x402 settle response

# Verify settlement was recorded
payer = feedback_client.verify_settlement(settlement_tx)
assert payer == signer.address

# Build and submit feedback
params = FeedbackParams(
    agent_id=agent_id,
    value=95,
    value_decimals=0,
    tag1="x402",
    tag2="weather",
    endpoint="https://example.com/weather",
    feedback_uri="",
    feedback_hash=Web3.solidity_keccak(["uint256", "bytes32"], [agent_id, settlement_tx]),
)

feedback_client.submit_feedback(params, settlement_tx)
```

## 9. Fork-Friendly Checklist

- [ ] Single directory: `python/x402/extensions/erc8004/`
- [ ] Clear separation: server extension vs client extension
- [ ] All addresses configurable via `ERC8004Config`
- [ ] Self-contained example with `README.md`
- [ ] No core x402 protocol changes
- [ ] Transport-agnostic (uses standard x402 extension field)
- [ ] Graceful fallback when extension not present
- [ ] Gas costs documented with future optimization pointers

## 10. Dependencies

- `x402[evm]` — EVM signer, Web3 interactions
- `web3.py` — Contract interactions
- `eth-account` — EIP-7702 authorization signing
- `foundry` — Contract compilation (build-time only)

## 11. Open Questions / Future Work

1. **TypeScript and Go ports** — Deferred to follow-up PRs
2. **Server-side auto-submission** — Resource server optionally submits feedback on client's behalf (requires signed authorization from client)
3. **Gas sponsorship** — See Section 6
4. **L2 deployments** — FeedbackGateway and ReputationRegistry on Base/Arbitrum
5. **Batch feedback** — Amortize gas across multiple feedback submissions
