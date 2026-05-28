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

[`main.py`](./main.py) is a complete, runnable demo: it starts a local Anvil,
performs a real settlement transfer, signs a real agent receipt, uploads the
artifact to **real IPFS via Pinata**, and submits `giveFeedback` **on-chain**.

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

One-time setup — install the SDK into the project venv (editable):

```bash
cd python/x402
uv pip install -e .
```

Then run the demo:

```bash
uv run python ../../examples/python/clients/erc8004/main.py
```

On success you'll see the real IPFS CID and the decoded on-chain feedback
transaction, ending in `verify_feedback -> FULL`:

```
CID:          bafkrei...
feedbackURI:  ipfs://bafkrei...
===== on-chain feedback transaction (Anvil) =====
  txHash:        0x...
  status:        1 (block 5)
  from (client): 0xf39Fd6...
  to (registry): 0x...
  giveFeedback.feedbackURI:  ipfs://bafkrei...
  giveFeedback.feedbackHash: 0x...
verify_feedback -> FULL
SUCCESS — feedback posted on-chain, artifact at ipfs://bafkrei...
```

Open the printed `https://<CID>.ipfs.inbrowser.link/` link to inspect the
uploaded artifact (real settlement `txHash` + real agent signature).

The same flow also runs as an integration test:
[`python/x402/tests/integration/test_erc8004_pinata_e2e.py`](../../../../python/x402/tests/integration/test_erc8004_pinata_e2e.py)

```bash
cd python/x402
uv run pytest tests/integration/test_erc8004_pinata_e2e.py -v -s -m integration
```

> The demo deploys three tiny mock contracts on the local Anvil (an ERC-20-style
> token, an `IdentityRegistry`, and a calldata-logging `ReputationRegistry`) so
> no Solidity compiler is needed. Against a real chain you'd point
> `ERC8004Config` at the deployed ERC-8004 registries instead.

## Using the API in your own code

- **Client** — the runnable demo above ([`main.py`](./main.py)) shows the full
  client flow: build + publish the artifact, then submit feedback. In your app
  the `requirements` / `payment_payload` / settlement `txHash` come from the
  normal x402 payment round-trip instead of a local mock.
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
