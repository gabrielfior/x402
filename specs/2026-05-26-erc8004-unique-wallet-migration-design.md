# ERC-8004 Unique Agent Wallet Migration — Design

**Status:** Draft
**Date:** 2026-05-26
**Author:** gabrielfior

## Problem

The ERC-8004 `IdentityRegistry` currently allows the same `agentWallet` address to back multiple `agentId`s (many-to-one). One developer EOA may control many agents through a shared wallet. We want a permanent **unique** mapping: each `agentId` resolves to a distinct `agentWallet`, and no address ever backs two agents.

This requires both a forward path for new registrations and a migration path for existing duplicates. Existing `agentId`s and any external references to them (reputation, feedback, links from other contracts) must be preserved — in-place migration only; no re-issue, no grandfathering.

## Non-goals

- Per-agent scoped signing authority (session keys, spending limits, paymasters). Each agent holding a single full-permission hot signing path is acceptable.
- Changing the developer/owner relationship to the NFT. NFT ownership semantics are unchanged.
- Off-chain reputation/feedback migration. Those are keyed by `agentId`, which is stable through this change.

## Approach: ERC-6551 token-bound account per `agentId`

Each agent's canonical wallet is the deterministic ERC-6551 token-bound account derived from:

- Canonical 6551 registry: `0x000000006551c19487814612e58FE06813775758`
- Account implementation: Tokenbound reference implementation (audited; pinned address recorded in the extension's constants module)
- `chainId` of the deployment
- NFT contract: the `IdentityRegistry` proxy address
- `tokenId`: the `agentId`
- `salt`: `0`

### Why this fits

- **Uniqueness is structural, not enforced.** Distinct `tokenId`s produce distinct TBA addresses by construction; collisions are mathematically impossible. The mapping is recoverable from the tokenId alone — no off-chain registry of "agentId → wallet salts" to maintain.
- **Ownership is intrinsic to the NFT.** Whoever owns the agent NFT controls the TBA. NFT transfer automatically transfers wallet control, mirroring the registry's existing `_update` hook that auto-clears `agentWallet` on transfer.
- **Signing works through existing `setAgentWallet`.** The registry's `setAgentWallet` already falls back to EIP-1271 if ECDSA recovery fails (verified against the deployed implementation). A standard TBA's `isValidSignature` delegates validity to the NFT owner's EOA signature, so the developer signs the digest with their existing EOA and the registry accepts it via the 1271 path. No registry change is needed for signature handling.

## Registry contract change

The only contract change is the uniqueness invariant. The `IdentityRegistry` is UUPS-upgradeable, so this lands as an implementation upgrade.

### Storage

Add a reverse mapping:

```
mapping(address => uint256) walletToAgentId;  // 0 == unassigned
```

`agentId` 0 is reserved as the sentinel (agent IDs are issued starting from 1; this should be confirmed against the current `_lastId` initialization and asserted in the upgrade migration).

### Invariant

For any non-zero `wallet`, `walletToAgentId[wallet]` is either `0` or the unique `agentId` whose `agentWallet` currently equals `wallet`.

### Behavior changes

- **`setAgentWallet(agentId, newWallet, deadline, signature)`**: revert if `walletToAgentId[newWallet] != 0 && walletToAgentId[newWallet] != agentId`. On success, clear the reverse entry for the previous `agentWallet[agentId]` (if non-zero) and set `walletToAgentId[newWallet] = agentId`.
- **`unsetAgentWallet(agentId)`**: clear the reverse entry for the current `agentWallet[agentId]` before zeroing it.
- **NFT transfer auto-clear** (`_update` override): clear the reverse entry in the same step as clearing `agentWallet`.

### Enforcement posture

Uniqueness is **live from the moment the upgrade is deployed**. There is no migration flag and no grace period in the contract itself.

Consequence: any `agentId` that, at upgrade time, shares its `agentWallet` with another `agentId` becomes effectively **read-only on the wallet field** — the developer cannot call `setAgentWallet` to point it at a *different* duplicate, and they cannot point another duplicate at this wallet. The only escape is to migrate it to a unique address (its TBA), which is exactly the desired behavior.

`unsetAgentWallet` remains callable (and clears the reverse entry), so a developer can also sunset an agent rather than migrating it.

### Upgrade-time backfill

The implementation upgrade must, as part of its initializer or a one-shot post-upgrade function, walk all existing `agentId`s and populate `walletToAgentId`. For duplicates, the *last writer wins* in the reverse map — meaning the reverse lookup will resolve to one of the colliding agents, and the others will appear "orphaned" in the reverse direction until migrated. This is acceptable: the forward `agentWallet[agentId]` mapping is untouched, so no reads break; the reverse map is only used for uniqueness checks on writes.

If the live agent count is small enough, backfill in the initializer. Otherwise, expose a paginated `backfillReverseIndex(startId, endId)` callable by the registry owner and call it post-upgrade until complete. Either way, this is a one-time operation.

## Migration execution

Migration is **developer-driven**. The EIP-1271 path on the TBA requires the NFT owner's signature; there is no administrative shortcut to migrate an agent without the developer's participation.

### Per-agent migration sequence

For each existing `agentId` owned by a developer:

1. **Compute** the TBA address from the canonical 6551 inputs above. Pure off-chain derivation.
2. **Deploy** the TBA via the 6551 registry's `createAccount` if not already deployed. Idempotent; ~30k gas. Required because `setAgentWallet`'s EIP-1271 fallback `staticcall`s the wallet, which only works if the contract exists.
3. **Sign** the `setAgentWallet` EIP-712 digest with the developer's EOA. The TBA's `isValidSignature` will return the magic value because the recovered signer matches the NFT owner.
4. **Submit** `setAgentWallet(agentId, tbaAddress, deadline, signature)`. The uniqueness check passes because the TBA address is unique by construction. The previous shared `agentWallet` entry is cleared from the reverse map; the new entry is written.

### Tooling delivered in this extension

- **CLI / script in `python/x402/extensions/erc8004`**: given a developer wallet address, enumerates owned `agentId`s (via `Transfer` event scan on the registry), computes each TBA address, deploys missing TBAs, builds the `setAgentWallet` calls, and submits them. Designed to run in a single developer session.
- **Optional meta-tx relayer (out of scope for v1)**: developer signs each `setAgentWallet` digest off-chain, a relayer pays gas. Listed here so the on-chain design does not foreclose it; not built in the initial migration release.
- **Forward-path integration**: the extension's "register new agent" flow auto-creates the TBA and calls `setAgentWallet` in the same flow. New agents are uniquely-walleted from inception; developers never see the indirection.

### Cutover

There is no on-chain cutover. The contract upgrade enables uniqueness immediately. Cutover is purely an external communication:

- Announce the upgrade window (≥90 days notice recommended) so developers can run the migration CLI before their duplicates become read-only on the wallet field.
- Surface a "your agent has a duplicated wallet" warning in any developer-facing tooling that reads the registry, until the duplicate is resolved.

## Components

| Component | Location | Responsibility |
|---|---|---|
| Registry implementation upgrade | `contracts/erc8004/` (or upstream PR to `erc-8004-contracts`) | Reverse mapping, uniqueness check, backfill |
| 6551 constants | `python/x402/extensions/erc8004/constants.py` | Pinned 6551 registry address, account implementation address, salt convention |
| TBA address derivation | `python/x402/extensions/erc8004/tba.py` (new) | Pure function: `(agentId) → tba_address` |
| Migration CLI | `python/x402/extensions/erc8004/migrate.py` (new) | Enumerate, derive, deploy-if-needed, sign, submit |
| Forward-path integration | existing `register_agent` flow in the extension | TBA derive + deploy + `setAgentWallet` in registration |
| Developer-facing warnings | x402 extension responses / docs | Surface duplicate-wallet status |

## Testing

- **Contract**: unit tests for `setAgentWallet` rejecting duplicates, accepting same-`agentId` re-points (idempotent), reverse map clearing on `unsetAgentWallet` and NFT transfer, backfill correctness on a fixture with known duplicates.
- **Migration CLI**: integration test against a forked-mainnet or local upgraded registry with a synthetic many-to-one fixture; assert N agents end at N distinct TBA addresses with NFT ownership unchanged.
- **TBA signing**: test that `setAgentWallet` accepts an EIP-712 signature from the developer EOA when `newWallet` is the developer's TBA, via the registry's 1271 path.
- **Read-only behavior post-upgrade**: assert that a pre-upgrade duplicate cannot be re-pointed to another duplicate's address; can only be pointed to a unique address.

## Open questions

- **6551 implementation pin.** Which Tokenbound implementation version? Pin a specific deployment address; document the constraint that it must implement standard `isValidSignature` delegating to the NFT owner.
- **Backfill size.** Need a count of live `agentId`s on mainnet to decide initializer-time vs paginated backfill.
- **Coordination with `erc-8004-contracts` upstream.** The uniqueness invariant is a change to the canonical registry. Is this an upstream PR (preferred) or a fork? If upstream, the design needs to land there first.

## Out of scope

- Session keys, paymasters, and per-agent scoped signing — covered by the "single hot key per agent is fine" decision; can be added later by developers pointing their agentWallet at a 4337 account if they want, without further registry changes.
- Cross-chain TBA derivation. This design assumes single-chain agent registration; multi-chain would require a separate discussion of TBA address consistency across chains.
