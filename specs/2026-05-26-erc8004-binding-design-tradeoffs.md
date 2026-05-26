# x402-erc8004 — Binding & Architecture Tradeoffs

Date: 2026-05-26
Status: design note

Companion to `2026-05-25-erc8004-extension-design.md`. Captures the decisions made
while iterating on (a) how to bind on-chain feedback to a specific x402 request,
(b) censorship-resistance against rogue agent/facilitator, and (c) whether
`FeedbackGateway` is still needed.

---

## 1. Ticket payload scope: `txHash`-only vs full request/response

| | On-chain ticket | Off-chain artifact |
|---|---|---|
| Carries | `{txHash, payer, agentId, nonce, sig}` | `paymentRequirements`, `paymentPayload`, `responseDigest`, `txHash`, signatures |
| Gas | Minimal | Zero (one `bytes32` commitment) |
| Binds tx ↔ x402 request | No (chain has no URLs) | Yes (hash committed via `feedbackHash`) |

**Decision:** on-chain ticket stays minimal; full binding lives off-chain. Putting payload bytes on-chain buys nothing — the chain has no notion of HTTP requests.

---

## 2. Binding mechanism

| Option | Pros | Cons |
|---|---|---|
| EIP-3009 `nonce = keccak(paymentRequirements)` | Trustless, client's own sig is the binding | USDC-only; doesn't generalize to Permit2 |
| **Off-chain canonical artifact + `feedbackHash`** ★ | Scheme-agnostic (EIP-3009 + Permit2 + any ERC-20); captures request *and* response; forward-compatible | Verifier work off-chain; URI durability is the weakest link |
| Settlement-through-gateway | No proofs needed; clean | Invasive to x402 settlement flow |
| Receipt-inclusion (MPT) proof | Fully trustless, no agent involvement | Complex; per-L2 blockhash plumbing |
| Facilitator co-signature | Soft trust signal | Moves censorship vector, doesn't remove it |

**Decision:** off-chain canonical artifact, hashed into `feedbackHash`, hosted at `feedbackURI`. Payment-scheme agnostic. Use content-addressed URIs (`ipfs://`, `ar://`).

---

## 3. Censorship resistance (rogue agent withholds signature)

| | Agent-signed only | Two-path (agent-signed + unilateral payer) ★ |
|---|---|---|
| Happy case | Works | Works |
| Agent refuses to sign | Client cannot submit | Client submits via payer path; binding intact |
| Response repudiation by agent | Possible | Optional agent signature on `responseDigest`; absence is itself evidence |

**Decision:** support both paths. Unilateral path requires only the client's existing payment signature + IdentityRegistry lookup.

---

## 4. Who uploads the artifact?

| | Agent uploads | **Client uploads** ★ |
|---|---|---|
| Censorship of feedback | Trivial (agent doesn't upload) | Impossible |
| Cost incentive | Misaligned (agent hosts feedback against itself) | Aligned (client pays for own feedback durability) |
| Response capture | Agent could swap body before publishing | Client publishes what it received |
| Agent dependency | Hard | None — agent provides optional 65-byte response signature only |

**Decision:** client uploads. Agent optionally returns `X-X402-Interaction-Receipt` header (one ECDSA sig over the response digest) at response time. If absent, client publishes a "client-only attestation" artifact — aggregators downgrade trust tier, don't reject.

---

## 5. Do we keep `FeedbackGateway`?

| | Keep current gateway | Minimal dedup-only gateway | **Remove entirely** ★ |
|---|---|---|---|
| LOC | ~100 + signature/ticket logic | ~25 | 0 |
| On-chain dedup | Yes (but bypassable via direct registry call) | Yes (bypassable) | No (off-chain only) |
| Trust model | Hybrid (on-chain sig + off-chain artifact) | Soft on-chain dedup + off-chain | Pure off-chain, honest |
| `msg.sender` on `NewFeedback` | Gateway address | Gateway address | Actual payer ✓ |
| Cross-chain deploy burden | Per chain | Per chain | None |
| Aligns with ERC-8004 design ethos | Partial | Partial | Yes |
| Aggregator must dedup anyway? | Yes (registry is permissionless) | Yes | Yes |

**Key insight:** `ReputationRegistry.giveFeedback` is permissionless. Any gateway is *bypassable* by calling the registry directly, so on-chain dedup is a soft constraint — aggregators must dedup off-chain regardless. The gateway only catches benign double-submits, not adversarial ones.

**Decision:** remove `FeedbackGateway`. The extension owns zero contracts.

- Dedup rule moves to the extension spec: `(payer, agentId, settlementTxHash) → latest by block`.
- Reference aggregator library (TS + Python) implements verification + dedup.
- Client SDK submits directly to `ReputationRegistry.giveFeedback`.

If the team objects, fall back to the **minimal dedup-only gateway** — never the current signature-checking one.

---

## Resulting extension surface

- **Contracts owned:** none.
- **Spec artifacts:**
  - Canonical artifact JSON schema (hosted at `feedbackURI`).
  - `X-X402-Interaction-Receipt` response header format.
  - Aggregator dedup + verification rules.
- **SDK helpers:** `build_and_publish_artifact(...)`, `submit_feedback_to_registry(...)`.
- **Trust model:** off-chain verifiable, payment-scheme agnostic, censorship-resistant.
