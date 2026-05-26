# ERC-8004 Feedback Extension — Examples

Submit verified, request-bound feedback to the standard ERC-8004
`ReputationRegistry` after an x402 payment. **No custom contracts** — the binding
between the payment and the feedback lives in an off-chain canonical artifact
(uploaded to IPFS) and is committed on-chain via `feedbackHash` / `feedbackURI`.

## How it works (gateway-less)

1. Client pays for the resource via the normal x402 flow → gets a settlement `txHash`.
2. (Optional) Server signs an `InteractionReceipt` over `{settlement, request, response}`
   digests and returns it in the `X-X402-Interaction-Receipt` header.
3. Client builds a canonical artifact `{settlement, interaction, feedback}`, hashes it
   into `feedbackHash`, and uploads it to IPFS → `feedbackURI` (`ipfs://<CID>`).
4. Client calls `ReputationRegistry.giveFeedback(...)` directly (plain type-2 tx);
   `msg.sender` is the payer, so attribution is correct without any gateway.
5. Anyone can verify off-chain: fetch `feedbackURI`, check the hash, confirm the
   ERC-20 `Transfer` in `txHash`, check `ownerOf(agentId)`, and (if present) the
   agent receipt → a `TrustTier`.

## Run the full end-to-end demo (real IPFS + on-chain on Anvil)

The integration test is a complete, runnable demo: it starts a local Anvil,
performs a real settlement transfer, signs a real agent receipt, uploads the
artifact to **real IPFS via Pinata**, and submits `giveFeedback` **on-chain**.

Test file:
[`python/x402/tests/integration/test_erc8004_pinata_e2e.py`](../../../../python/x402/tests/integration/test_erc8004_pinata_e2e.py)

### Requirements

- **Foundry** (`anvil` on your `PATH`) — https://book.getfoundry.sh/getting-started/installation
- **uv** — https://docs.astral.sh/uv/
- A **Pinata JWT** with file-upload scope, placed in the repo-root `.env`:

  ```dotenv
  # x402/.env
  PINATA_JWT=eyJhbGciOi...
  ```

  (Get one at https://app.pinata.cloud → API Keys.)

### Run

```bash
cd python/x402
uv run pytest tests/integration/test_erc8004_pinata_e2e.py -v -s -m integration
```

The `-s` flag is what surfaces the live output. On success you'll see the real
IPFS CID and the decoded on-chain feedback transaction, e.g.:

```
[e2e] CID:          bafkrei...
[e2e] feedbackURI:  ipfs://bafkrei...
[e2e] ===== on-chain feedback transaction (Anvil) =====
[e2e]   txHash:        0x...
[e2e]   status:        1 (block 5)
[e2e]   from (client): 0xf39Fd6...
[e2e]   to (registry): 0x...
[e2e]   giveFeedback.feedbackURI:  ipfs://bafkrei...
[e2e]   giveFeedback.feedbackHash: 0x...
[e2e] verify_feedback -> FULL
```

Open the printed `https://<CID>.ipfs.inbrowser.link/` link to inspect the
uploaded artifact (real settlement `txHash` + real agent signature).

> The demo deploys three tiny mock contracts on the local Anvil (an ERC-20-style
> token, an `IdentityRegistry`, and a calldata-logging `ReputationRegistry`) so
> no Solidity compiler is needed. Against a real chain you'd point
> `ERC8004Config` at the deployed ERC-8004 registries instead.

## Using the API in your own code

- **Client** — pay, build + publish the artifact, submit feedback: [`main.py`](./main.py)
- **Server** — declare `agentId` and sign the interaction receipt at the HTTP
  layer: [`server_example.py`](./server_example.py)

```bash
pip install x402[evm]   # or: uv add x402[evm]
```

## Production prerequisites

- An agent registered on the ERC-8004 `IdentityRegistry` (you control
  `ownerOf(agentId)`).
- The `ReputationRegistry` / `IdentityRegistry` addresses for your chain, set in
  `ERC8004Config`.
- A funded client EOA (pays gas for `giveFeedback`).
- A `PINATA_JWT` (or any `ArtifactUploader`; prefer content-addressed IPFS/Arweave).
