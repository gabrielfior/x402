# ERC-8004 Extension Design Review

**Review of:** `2026-05-25-erc8004-extension-design.md`
**Date:** 2026-05-25

---

## Critical: Protocol-Level Violations

### 1. Extension value missing `{ info, schema }` wrapper

**Location:** Sections 4.1, 5.2

The design puts `agentId` directly in `extensions["erc8004"]`:

```python
extensions={"erc8004": {"agentId": 42}}
```

The x402 v2 spec **requires** every extension value to follow:

| Field    | Type     | Required | Description                                           |
| -------- | -------- | -------- | ----------------------------------------------------- |
| `info`   | `object` | Required | Extension-specific data provided by the server        |
| `schema` | `object` | Required | JSON Schema defining the expected structure of `info` |

The existing `payment-identifier` extension is the canonical reference — its `declare_payment_identifier_extension()` returns `{"info": {"required": ...}, "schema": payment_identifier_schema}`. Without this structure, the extension will be rejected by spec-compliant clients and facilitators.

**Fix:** Change the extension value to `{"info": {"agentId": 42}, "schema": {"$ref": "https://json-schema.org/draft/2020-12/schema", "type": "object", "properties": {"agentId": {"type": "integer"}}, "required": ["agentId"]}}`.

---

### 2. `IReputationRegistry.giveFeedback` signature — matches deployed contract

**Location:** Section 5.5

The design spec's `IReputationRegistry` interface:
```solidity
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
```

This **correctly matches** the actual deployed ERC-8004 `ReputationRegistry` at `0x8004BAa1...` on mainnet (`0x8004B663...` on Sepolia). The deployed `ReputationRegistryUpgradeable.sol` uses `int128 value, uint8 valueDecimals, string tag1, string tag2, string endpoint, string feedbackURI, bytes32 feedbackHash` — a 1:1 match.

The `feedbackAuth` parameter was present in earlier ERC-8004 draft specs but was **removed in the deployed contract**. The design correctly omits it.

**Sources:** [`erc-8004/erc-8004-contracts`](https://github.com/erc-8004/erc-8004-contracts), [`tetratorus/erc-8004-py`](https://github.com/tetratorus/erc-8004-py)

---

## Significant Design Problems

### 3. `feedback_hash` semantics conflict with ERC-8004

**Location:** Section 5.2

The design uses `feedback_hash = keccak256(agentId, settlementTxHash)` as a payment-to-feedback binding proof and dedup key on `FeedbackGateway`.

**What ERC-8004 expects `feedbackHash` to be:** The keccak256 hash of the **off-chain feedback JSON file** at `feedbackURI` — a file integrity check. Anyone can fetch the file, hash it, and compare against on-chain storage to verify the content hasn't been tampered with. For IPFS URIs it can be `bytes32(0)` (content-addressed).

**Why this breaks:** Any on-chain reputation indexer, subgraph, auditor, or dApp that reads `feedbackHash` will try to verify it against content at `feedbackURI`. They'll compute `keccak256(file_bytes)` and get a mismatch, concluding the data is corrupted. The hash is meaningless for its intended purpose.

**Decision: Option (a) — split into two hashes on `FeedbackGateway`**

The `FeedbackGateway` contract should accept a separate `settlementBindingHash` (or similar) for dedup/payment-binding, and pass `bytes32(0)` to `ReputationRegistry.giveFeedback` as `feedbackHash`:

```
FeedbackGateway.submitFeedback(
    agentId, params, settlementTxHash
) →
    1. Verify dedup via internal settlementBindingHash map
    2. Pass bytes32(0) as feedbackHash to ReputationRegistry.giveFeedback(...)
    3. Emit FeedbackGateway-specific event linking settlementTxHash to feedback
```

This cleanly separates concerns:
- `feedbackHash` on ERC-8004 → file integrity (used as intended, or set to `bytes32(0)`)
- `settlementBindingHash` on `FeedbackGateway` → payment binding (used for dedup and proof)

---

### 4. No `PaymentPayload.extensions` echo specified

**Location:** Sections 4, 5

The x402 v2 spec (`specs/x402-specification-v2.md:146`) states:

> *"The client must include at least the info received; it may append additional info but cannot delete or overwrite existing info."*

The design has the client extracting `agentId` from the 402 response (Section 4.2, step 1), but never mentions echoing it back in `PaymentPayload.extensions["erc8004"]`. This is a mandatory protocol requirement.

**Why it matters:** Without the echo, the server's `enrich_settlement_response` and lifecycle hooks (like `on_after_settle`) may not fire. The framework dispatches extension callbacks based on keys present in *both* `PaymentPayload.extensions` and the route declaration. If the key is missing from `PaymentPayload`, the server-side extension hooks won't execute.

**Fix — follow the `payment-identifier` utility pattern:**

```python
def echo_erc8004_in_payment_payload(
    payment_payload: PaymentPayload,
    payment_required: PaymentRequired,
) -> PaymentPayload:
    """Echo the erc8004 extension into PaymentPayload per x402 v2 spec."""
    if not payment_required.extensions or "erc8004" not in payment_required.extensions:
        return payment_payload

    ext = payment_required.extensions["erc8004"]
    info_copy = deepcopy(ext)  # preserves {info, schema} wrapper

    extensions = dict(payment_payload.extensions or {})
    extensions["erc8004"] = info_copy
    payment_payload.extensions = extensions
    return payment_payload
```

Or wired as a `ClientExtension`:
```python
class ERC8004ClientExtension:
    key = "erc8004"

    def enrich_payment_payload(self, payment_payload, payment_required):
        return echo_erc8004_in_payment_payload(payment_payload, payment_required)
```

---

### 5. `recordSettlement` is permissionless / first-writer wins — front-running risk

**Location:** Section 5.5

> `recordSettlement(bytes32 txHash, address payer)` — permissionless, first-writer wins

**The attack:** An observer sees the settlement tx in the mempool. They front-run with `recordSettlement(txHash, attacker_address)`. Now:
- Client polls `settlementPayer(txHash)` → returns `attacker_address`
- Client check `payer == self` **fails** — legitimate client can't submit feedback
- Worse: attacker uses someone else's settlement tx to submit feedback for their own agent, poisoning the reputation signal with a fake "paid" endorsement

**Why the fix matters:** The `recordSettlement` call must authorize the `(txHash, payer)` binding. Only the server knows which client paid. Anyone else observing the tx hash on-chain cannot know the intended payer.

**Fix — require EIP-712 server signature:**

```solidity
function recordSettlement(
    bytes32 txHash,
    address payer,
    bytes calldata serverSignature  // EIP-712 signature over (txHash, payer)
) external {
    address signer = ECDSA.recover(
        keccak256(abi.encodePacked(txHash, payer)),
        serverSignature
    );
    require(signer == serverAuthority, "invalid server signature");
    settlements[txHash] = payer;
}
```

**Key properties:**
- Server is the only party that can authorize a `(txHash, payer)` binding
- Server only signs AFTER settlement completes (in `on_after_settle`)
- No front-running possible: the server's signature binds the specific payer
- Client can independently verify the server sig off-chain before submitting feedback
- Server's authority address is a well-known config parameter (part of `ERC8004Config`)

---

## Moderate Issues

### 6. `tryClaimSettlement` is confusingly described

**Location:** Section 5.5

> `tryClaimSettlement(bytes32 txHash, address caller)` — internal, called via external call to dedupStore

The name `tryClaimSettlement` suggests settlement claiming, not deduplication. What is `dedupStore`? It's introduced without definition. This appears to conflate two separate concerns (settlement verification and deduplication).

---

### 7. Gas cost estimates for EIP-7702 are overstated

**Location:** Section 6

> Submitting feedback requires an EIP-7702 type-4 transaction, which costs gas. On Ethereum mainnet this could range from $5–50+ depending on network congestion.

EIP-7702 type-4 transactions delegate code to an existing contract — they do **not** deploy new bytecode. The actual cost is roughly a base L1 tx (~21k gas) plus calldata for the delegation (~10k), totaling ~$1-3 at 10 gwei. The stated range of $5–50 seems confused with regular contract deployment or complex contract interaction.

---

### 8. No RPC polling failure handling

**Location:** Section 4.2, step 3

The client polls `FeedbackGateway.settlementPayer(txHash)` with no documented:
- Polling interval
- Timeout duration
- Backoff strategy
- Error path if the server never calls `recordSettlement`

Since `recordSettlement` failure "is logged as warning, never blocks the settlement response" (Section 5.3), this polling could hang indefinitely. The design should specify retry limits and a graceful degradation path.

---

### 9. EIP-7702 authorizer abstraction — support more wallet types

**Location:** Section 5.4

`ERCFeedbackClient.__init__(self, config, signer: EthAccountSigner)` constrains to a single concrete signer. But users will want to use browser wallets, hardware wallets, or RPC-backed signers. EIP-7702 type-4 transactions require the EOA to sign an `Authorization` tuple `(chainId, address, nonce)` — the resulting `(yParity, r, s)` is embedded in the tx. Contract wallets (Safes, ERC-4337) **cannot** produce this at all.

**Fix — define an `EIP7702Authorizer` protocol + multiple implementations:**

```python
@runtime_checkable
class EIP7702Authorizer(Protocol):
    """Anything that can produce an EIP-7702 authorization tuple."""
    @property
    def address(self) -> str: ...

    def sign_authorization(
        self,
        chain_id: int,
        delegate_to: str,  # FeedbackGateway address
        nonce: int,
    ) -> dict:  # -> { yParity, r, s } or raw RLP-encoded tx
        ...
```

| Implementation | Source | How it signs |
|---------------|--------|-------------|
| `EthAccountEIP7702Authorizer` | Local private key | `account.sign_typed_data()` on authorization tuple |
| `RPCEIP7702Authorizer` | Remote RPC / browser wallet | `eth_sendTransaction({type: "0x4", ...})` or `eth_signTypedData` |
| `Web3EIP7702Authorizer` | Web3.py with RPC | Same as local with RPC nonce fetching |

Parameterize `ERCFeedbackClient.__init__` with this protocol and ship `EthAccountEIP7702Authorizer` as the default. Document that hardware wallets / browser wallets need their own adapter.

**Smart wallet gap — Option A as alternative:**

Non-EOA wallets (Safes, ERC-4337) can't do EIP-7702 at all. For these, add a **server-side auto-submission** path (already listed in Section 11, item 2 of the design):

1. Client signs an EIP-712 authorization for the server to call `giveFeedback` on their behalf
2. Server submits the feedback tx (server pays gas)
3. On-chain feedback is attributed to the client's smart wallet address
4. No EIP-7702 needed — standard contract interaction from the server

This should be documented as the recommended fallback for smart wallet users, with implementation deferred to a follow-up PR.

---

## Minor / Nitpicks

| Issue | Location | Detail |
| ----- | -------- | ------ |
| `feedback_hash: bytes` should be `bytes32` | Section 5.2 | Using bare `bytes` is ambiguous for a fixed 32-byte value |
| `identity_registry` in config is never used | Section 5.2 | `ERC8004Config` includes `identity_registry` but no shown method references it |
| `tag1`/`tag2` type mismatch | Section 5.2 | ERC-8004 deployed contracts use `bytes32` for tags, design uses `string` |
| Polling race with `on_after_settle` | Section 4.3 | `on_after_settle` runs *after* the 200 response is sent, so the client's polling naturally races with the server recording settlement |
| `int128` vs `uint8` | Section 5.5 | EIP-8004 spec uses `int128` for future-proofing, but earlier deployed versions used `uint8 score` — design matches the latest deployed contract correctly with `int128` |
