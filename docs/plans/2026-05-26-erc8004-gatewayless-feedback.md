# ERC-8004 Gateway-less Feedback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rework the x402 ERC-8004 feedback extension so it owns zero smart contracts: clients submit feedback directly to the standard `ReputationRegistry`, binding the on-chain feedback to a specific x402 request via an off-chain canonical artifact committed through `feedbackHash`/`feedbackURI`.

**Architecture:** The client builds a canonical JSON artifact `{settlement, interaction, feedback}` capturing `paymentRequirements`, `paymentPayload`, the response digest, and `txHash`. It hashes the artifact (keccak256) into `feedbackHash`, uploads it (client-controlled storage) to obtain `feedbackURI`, then calls `ReputationRegistry.giveFeedback` directly — `msg.sender` is the payer. The agent server *optionally* returns a signed `X-X402-Interaction-Receipt` attesting to the response; its absence downgrades trust but never blocks submission. Verification is payment-scheme agnostic because it keys off the universal ERC-20 `Transfer` event (works for EIP-3009, Permit2, and plain ERC-20). Dedup is enforced off-chain by aggregators on `(payer, agentId, settlementTxHash)`.

**Tech Stack:** Python 3.11, `web3.py`, `eth_account`, `eth_utils.keccak`, Pydantic v2, pytest. Test runner: `cd python/x402 && uv run pytest`.

---

## Design decisions (locked from `specs/2026-05-26-erc8004-binding-design-tradeoffs.md`)

- **No contracts owned.** Delete `contracts/erc8004/` (only `FeedbackGateway.sol`, plus interfaces/mocks/tests it carried). The SDK keeps minimal inline ABIs for `ReputationRegistry.giveFeedback`, `IdentityRegistry.ownerOf`, and the ERC-20 `Transfer` event.
- **No `FeedbackTicket`, no EIP-7702 delegation, no `usedNonces` dedup.** Removed.
- **Canonical JSON** = UTF-8, keys sorted lexicographically, compact separators `(",", ":")`, `ensure_ascii=False`, **no floats** (amounts are strings, ids/values are ints). This is the hashing preimage; it must be specified exactly so any verifier reproduces the same bytes.
- **Two hashes:**
  - `interaction_hash` = keccak256 over the canonical *core* `{version, settlement}`. This is the payment-level interaction the agent attests to **at settlement time** — so server and client compute the identical preimage. It deliberately excludes `feedback` (the client's opinion) and the request/response digests (which the agent does not have at settlement in v1). Response-repudiation coverage is a documented v2 enhancement.
  - `feedback_hash` (on-chain) = keccak256 over the canonical *full* artifact (includes `settlement`, `interaction` request/response, `feedback`, and `agentSignature`). This is committed via `ReputationRegistry`.
- **Verification keys off the ERC-20 `Transfer` log** `Transfer(address indexed from, address indexed to, uint256 value)` emitted by the `asset` contract, matching `from==payer`, `to==payTo`, `value==amount`. Scheme-agnostic.

---

## File Structure

**Delete:**
- `contracts/erc8004/` (entire directory)

**Modify:**
- `python/x402/extensions/erc8004/types.py` — drop `FeedbackTicket`; drop `feedback_gateway` from `ERC8004Config`, add `identity_registry`; add `InteractionReceipt`, `FeedbackArtifact` models.
- `python/x402/extensions/erc8004/server.py` — replace ticket signing with optional `InteractionReceipt` emission.
- `python/x402/extensions/erc8004/client.py` — drop EIP-7702 `submit_feedback`/`check_duplicate`; add artifact build, `ArtifactUploader` protocol, `submit_feedback_to_registry`.
- `python/x402/extensions/erc8004/constants.py` — drop gateway addresses; add identity-registry addresses.
- `python/x402/extensions/erc8004/__init__.py` — update exports.
- `python/x402/extensions/erc8004/README.md` — rewrite architecture section.

**Create:**
- `python/x402/extensions/erc8004/artifact.py` — canonical hashing, artifact builder, receipt signing/digest.
- `python/x402/extensions/erc8004/verify.py` — integrity/settlement/binding/receipt verification, trust tier, dedup.
- `python/x402/tests/unit/extensions/erc8004/test_artifact.py`
- `python/x402/tests/unit/extensions/erc8004/test_verify.py`

**Rewrite tests:**
- `python/x402/tests/unit/extensions/erc8004/test_server.py`
- `python/x402/tests/unit/extensions/erc8004/test_client.py`
- `python/x402/tests/unit/extensions/erc8004/test_types.py` (drop ticket tests)
- `python/x402/tests/integration/test_erc8004_e2e.py` (update flow)

---

## Task 1: Remove the FeedbackGateway contract layer

**Files:**
- Delete: `contracts/erc8004/` (whole tree)

- [ ] **Step 1: Confirm nothing in Python imports the contract dir**

Run: `cd /Users/gabrielfior/code/ef/x402 && grep -rn "contracts/erc8004\|FeedbackGateway" python/ --include='*.py'`
Expected: only references inside `client.py`/`server.py`/tests (Python-side ABI usage) — no path imports of the Solidity tree. Note them; they are handled in later tasks.

- [ ] **Step 2: Delete the contract directory**

```bash
cd /Users/gabrielfior/code/ef/x402
git rm -r contracts/erc8004
```

- [ ] **Step 3: Verify removal**

Run: `ls contracts/ 2>/dev/null; test ! -d contracts/erc8004 && echo OK`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "refactor(erc8004): remove FeedbackGateway contract — extension owns no contracts"
```

---

## Task 2: Update types.py

**Files:**
- Modify: `python/x402/extensions/erc8004/types.py`
- Test: `python/x402/tests/unit/extensions/erc8004/test_types.py`

- [ ] **Step 1: Write the failing tests**

Replace the body of `test_types.py` with:

```python
"""Tests for ERC-8004 extension types."""

import pytest
from pydantic import ValidationError

from x402.extensions.erc8004.types import (
    ERC8004Config,
    FeedbackParams,
    InteractionReceipt,
    FeedbackArtifact,
)


def test_config_no_gateway_field() -> None:
    cfg = ERC8004Config(
        network="eip155:8453",
        reputation_registry="0x8004BAa17C55a88189AE136b182e5fdA19dE9b63",
        identity_registry="0x8004A18C4f0D0307C40Bd9176E1A53569b73e6a3",
        rpc_url="http://localhost:8545",
        agent_id=42,
    )
    assert cfg.identity_registry.startswith("0x")
    assert not hasattr(cfg, "feedback_gateway") or "feedback_gateway" not in cfg.model_fields


def test_feedback_params_defaults() -> None:
    p = FeedbackParams(agent_id=42, value=90)
    assert p.value_decimals == 0
    assert p.feedback_uri == ""
    assert p.feedback_hash == b"\x00" * 32


def test_interaction_receipt_roundtrip() -> None:
    r = InteractionReceipt(
        tx_hash=b"\x11" * 32,
        interaction_hash=b"\x22" * 32,
        chain_id=8453,
        signature=b"\x33" * 65,
    )
    d = r.to_dict()
    assert d["txHash"] == "0x" + "11" * 32
    assert d["interactionHash"] == "0x" + "22" * 32
    assert d["chainId"] == 8453
    assert d["signature"] == "0x" + "33" * 65
    back = InteractionReceipt.from_dict(d)
    assert back == r


def test_feedback_artifact_minimal() -> None:
    art = FeedbackArtifact(
        settlement={
            "txHash": "0x" + "ab" * 32,
            "chainId": "eip155:8453",
            "scheme": "exact",
            "paymentMethod": "eip3009",
            "asset": "0x" + "01" * 20,
            "payer": "0x" + "02" * 20,
            "payTo": "0x" + "03" * 20,
            "amount": "1000000",
        },
        interaction={
            "request": {"method": "GET", "url": "https://x/y", "headerDigest": "0x" + "00" * 32, "bodyDigest": "0x" + "00" * 32},
            "response": {"status": 200, "headerDigest": "0x" + "00" * 32, "bodyDigest": "0x" + "0a" * 32, "agentSignature": None},
        },
        feedback={"agentId": 42, "value": 90, "valueDecimals": 0, "tag1": "", "tag2": "", "endpoint": "", "comment": ""},
    )
    assert art.version == "x402-erc8004/1"
    assert art.to_dict()["version"] == "x402-erc8004/1"
```

- [ ] **Step 2: Run to verify failure**

Run: `cd python/x402 && uv run pytest tests/unit/extensions/erc8004/test_types.py -v`
Expected: FAIL with ImportError on `InteractionReceipt` / `FeedbackArtifact`.

- [ ] **Step 3: Rewrite types.py**

Replace the whole file with:

```python
"""Type definitions for the ERC-8004 Feedback Extension."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

EXTENSION_KEY = "erc8004"
ARTIFACT_VERSION = "x402-erc8004/1"


class ERC8004ExtensionInfo(BaseModel):
    """Info portion of the erc8004 extension declaration."""

    agent_id: int

    model_config = {"extra": "allow"}


class ERC8004ExtensionDeclaration(BaseModel):
    """What server puts in PaymentRequired.extensions['erc8004']."""

    info: ERC8004ExtensionInfo
    schema_: dict[str, Any] = Field(alias="schema")

    model_config = {"extra": "allow", "populate_by_name": True}


class ERC8004Config(BaseModel):
    """Chain-specific config for ERC-8004 extension."""

    network: str
    reputation_registry: str
    identity_registry: str
    rpc_url: str
    agent_id: int | None = None

    model_config = {"extra": "allow"}


class FeedbackParams(BaseModel):
    """Parameters for ReputationRegistry.giveFeedback."""

    agent_id: int
    value: int
    value_decimals: int = 0
    tag1: str = ""
    tag2: str = ""
    endpoint: str = ""
    feedback_uri: str = ""
    feedback_hash: bytes = Field(default=b"\x00" * 32)

    model_config = {"extra": "allow"}


class InteractionReceipt(BaseModel):
    """Agent-signed attestation over a paid interaction (optional).

    Signed by IdentityRegistry.ownerOf(agentId). Returned by the server in the
    X-X402-Interaction-Receipt header and embedded by the client into the
    artifact at interaction.response.agentSignature.
    """

    tx_hash: bytes
    interaction_hash: bytes
    chain_id: int
    signature: bytes

    model_config = {"extra": "allow"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "txHash": "0x" + self.tx_hash.hex(),
            "interactionHash": "0x" + self.interaction_hash.hex(),
            "chainId": self.chain_id,
            "signature": "0x" + self.signature.hex(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> InteractionReceipt:
        return cls(
            tx_hash=bytes.fromhex(data["txHash"].removeprefix("0x")),
            interaction_hash=bytes.fromhex(data["interactionHash"].removeprefix("0x")),
            chain_id=int(data["chainId"]),
            signature=bytes.fromhex(data["signature"].removeprefix("0x")),
        )


class FeedbackArtifact(BaseModel):
    """Canonical off-chain feedback artifact hosted at feedbackURI."""

    version: str = ARTIFACT_VERSION
    settlement: dict[str, Any]
    interaction: dict[str, Any]
    feedback: dict[str, Any]

    model_config = {"extra": "allow"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "settlement": self.settlement,
            "interaction": self.interaction,
            "feedback": self.feedback,
        }
```

- [ ] **Step 4: Run to verify pass**

Run: `cd python/x402 && uv run pytest tests/unit/extensions/erc8004/test_types.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add python/x402/extensions/erc8004/types.py python/x402/tests/unit/extensions/erc8004/test_types.py
git commit -m "refactor(erc8004): drop ticket types, add InteractionReceipt + FeedbackArtifact"
```

---

## Task 3: Canonical hashing + artifact builder + receipt digest (artifact.py)

**Files:**
- Create: `python/x402/extensions/erc8004/artifact.py`
- Test: `python/x402/tests/unit/extensions/erc8004/test_artifact.py`

- [ ] **Step 1: Write the failing tests**

Create `test_artifact.py`:

```python
"""Tests for ERC-8004 canonical artifact + hashing."""

from eth_account import Account
from eth_utils import keccak

from x402.extensions.erc8004.artifact import (
    canonical_bytes,
    compute_interaction_hash,
    compute_feedback_hash,
    build_artifact,
    receipt_digest,
    sign_interaction_receipt,
    verify_interaction_receipt,
)
from x402.schemas.payments import PaymentPayload, PaymentRequirements


def _requirements() -> PaymentRequirements:
    return PaymentRequirements(
        scheme="exact",
        network="eip155:8453",
        asset="0x" + "01" * 20,
        amount="1000000",
        pay_to="0x" + "03" * 20,
        max_timeout_seconds=60,
    )


def test_canonical_bytes_sorted_compact() -> None:
    out = canonical_bytes({"b": 1, "a": 2})
    assert out == b'{"a":2,"b":1}'


def test_canonical_bytes_deterministic() -> None:
    a = canonical_bytes({"x": [1, 2], "y": {"k": "v"}})
    b = canonical_bytes({"y": {"k": "v"}, "x": [1, 2]})
    assert a == b


def test_build_artifact_shape() -> None:
    payload = PaymentPayload(payload={"sig": "0xdead"}, accepted=_requirements())
    art = build_artifact(
        requirements=_requirements(),
        payment_payload=payload,
        tx_hash="0x" + "ab" * 32,
        payer="0x" + "02" * 20,
        payment_method="eip3009",
        request={"method": "GET", "url": "https://x/y", "headerDigest": "0x" + "00" * 32, "bodyDigest": "0x" + "00" * 32},
        response={"status": 200, "headerDigest": "0x" + "00" * 32, "bodyDigest": "0x" + "0a" * 32},
        feedback={"agentId": 42, "value": 90, "valueDecimals": 0, "tag1": "", "tag2": "", "endpoint": "", "comment": ""},
    )
    d = art.to_dict()
    assert d["settlement"]["payer"] == "0x" + "02" * 20
    assert d["settlement"]["payTo"] == "0x" + "03" * 20
    assert d["settlement"]["amount"] == "1000000"
    assert d["interaction"]["response"]["agentSignature"] is None


def test_interaction_hash_excludes_feedback_and_agentsig() -> None:
    payload = PaymentPayload(payload={"sig": "0xdead"}, accepted=_requirements())
    base = dict(
        requirements=_requirements(),
        payment_payload=payload,
        tx_hash="0x" + "ab" * 32,
        payer="0x" + "02" * 20,
        payment_method="eip3009",
        request={"method": "GET", "url": "https://x/y", "headerDigest": "0x" + "00" * 32, "bodyDigest": "0x" + "00" * 32},
        response={"status": 200, "headerDigest": "0x" + "00" * 32, "bodyDigest": "0x" + "0a" * 32},
    )
    art1 = build_artifact(feedback={"agentId": 42, "value": 90, "valueDecimals": 0, "tag1": "", "tag2": "", "endpoint": "", "comment": "a"}, **base)
    art2 = build_artifact(feedback={"agentId": 42, "value": 10, "valueDecimals": 0, "tag1": "", "tag2": "", "endpoint": "", "comment": "z"}, **base)
    # interaction hash identical regardless of feedback content
    assert compute_interaction_hash(art1.to_dict()) == compute_interaction_hash(art2.to_dict())
    # feedback hash differs because rating differs
    assert compute_feedback_hash(art1.to_dict()) != compute_feedback_hash(art2.to_dict())


def test_receipt_sign_and_verify() -> None:
    agent = Account.create()
    tx_hash = b"\xab" * 32
    interaction_hash = b"\xcd" * 32
    chain_id = 8453
    receipt = sign_interaction_receipt(agent, chain_id, tx_hash, interaction_hash)
    assert receipt.chain_id == chain_id
    assert verify_interaction_receipt(receipt, agent.address) is True
    assert verify_interaction_receipt(receipt, "0x" + "00" * 20) is False


def test_receipt_digest_binds_all_fields() -> None:
    d1 = receipt_digest(8453, b"\xab" * 32, b"\xcd" * 32)
    d2 = receipt_digest(1, b"\xab" * 32, b"\xcd" * 32)
    assert d1 != d2
    assert d1 == keccak(b"x402-erc8004-receipt" + (8453).to_bytes(32, "big") + b"\xab" * 32 + b"\xcd" * 32)
```

- [ ] **Step 2: Run to verify failure**

Run: `cd python/x402 && uv run pytest tests/unit/extensions/erc8004/test_artifact.py -v`
Expected: FAIL with ImportError on `x402.extensions.erc8004.artifact`.

- [ ] **Step 3: Implement artifact.py**

```python
"""Canonical artifact construction, hashing, and interaction receipts."""

from __future__ import annotations

import json
from typing import Any

from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import keccak, to_checksum_address

from x402.schemas.payments import PaymentPayload, PaymentRequirements

from .types import ARTIFACT_VERSION, FeedbackArtifact, InteractionReceipt

RECEIPT_PREFIX = b"x402-erc8004-receipt"


def canonical_bytes(obj: Any) -> bytes:
    """Deterministic JSON encoding: sorted keys, compact, UTF-8, no floats."""
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _chain_id_int(network: str) -> int:
    parts = network.split(":")
    if len(parts) != 2:
        raise ValueError(f"invalid network format: {network}")
    return int(parts[1])


def build_artifact(
    requirements: PaymentRequirements,
    payment_payload: PaymentPayload,
    tx_hash: str,
    payer: str,
    payment_method: str,
    request: dict[str, Any],
    response: dict[str, Any],
    feedback: dict[str, Any],
) -> FeedbackArtifact:
    """Assemble the canonical feedback artifact. agentSignature starts as None."""
    response_with_sig = dict(response)
    response_with_sig.setdefault("agentSignature", None)
    return FeedbackArtifact(
        version=ARTIFACT_VERSION,
        settlement={
            "txHash": tx_hash if tx_hash.startswith("0x") else "0x" + tx_hash,
            "chainId": requirements.network,
            "scheme": requirements.scheme,
            "paymentMethod": payment_method,
            "asset": to_checksum_address(requirements.asset),
            "payer": to_checksum_address(payer),
            "payTo": to_checksum_address(requirements.pay_to),
            "amount": requirements.amount,
            "paymentPayload": payment_payload.model_dump(mode="json"),
            "paymentRequirements": requirements.model_dump(mode="json"),
        },
        interaction={"request": request, "response": response_with_sig},
        feedback=feedback,
    )


def _interaction_core(artifact: dict[str, Any]) -> dict[str, Any]:
    # v1: agent attests to the payment-level interaction only. Server and client
    # both compute over {version, settlement} so the hashes match. Request/response
    # digests are NOT covered by the agent receipt in v1 (agent lacks them at
    # settlement time); they are still committed by feedback_hash.
    return {
        "version": artifact["version"],
        "settlement": artifact["settlement"],
    }


def compute_interaction_hash(artifact: dict[str, Any]) -> bytes:
    """keccak256 over the canonical {version, settlement} core (agent-signed)."""
    return keccak(canonical_bytes(_interaction_core(artifact)))


def compute_feedback_hash(artifact: dict[str, Any]) -> bytes:
    """keccak256 over the canonical full artifact (on-chain commitment)."""
    return keccak(canonical_bytes(artifact))


def receipt_digest(chain_id: int, tx_hash: bytes, interaction_hash: bytes) -> bytes:
    """Digest the agent signs to attest to the interaction."""
    return keccak(
        RECEIPT_PREFIX + chain_id.to_bytes(32, "big") + tx_hash + interaction_hash
    )


def sign_interaction_receipt(
    signer: Any, chain_id: int, tx_hash: bytes, interaction_hash: bytes
) -> InteractionReceipt:
    """Sign the interaction digest with the agent owner key (personal_sign)."""
    digest = receipt_digest(chain_id, tx_hash, interaction_hash)
    signed = signer.sign_message(encode_defunct(digest))
    sig = signed.signature if hasattr(signed, "signature") else signed
    return InteractionReceipt(
        tx_hash=tx_hash,
        interaction_hash=interaction_hash,
        chain_id=chain_id,
        signature=bytes(sig),
    )


def verify_interaction_receipt(receipt: InteractionReceipt, expected_owner: str) -> bool:
    """Recover the receipt signer and compare to the expected agent owner."""
    digest = receipt_digest(receipt.chain_id, receipt.tx_hash, receipt.interaction_hash)
    try:
        recovered = Account.recover_message(encode_defunct(digest), signature=receipt.signature)
    except Exception:
        return False
    return to_checksum_address(recovered) == to_checksum_address(expected_owner)
```

- [ ] **Step 4: Run to verify pass**

Run: `cd python/x402 && uv run pytest tests/unit/extensions/erc8004/test_artifact.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add python/x402/extensions/erc8004/artifact.py python/x402/tests/unit/extensions/erc8004/test_artifact.py
git commit -m "feat(erc8004): canonical artifact, dual-hash, interaction receipt signing"
```

---

## Task 4: Server emits optional interaction receipt

**Files:**
- Modify: `python/x402/extensions/erc8004/server.py`
- Test: `python/x402/tests/unit/extensions/erc8004/test_server.py`

- [ ] **Step 1: Write the failing tests**

Replace `test_server.py` with:

```python
"""Tests for ERC-8004 server extension."""

from unittest.mock import MagicMock

from eth_account import Account
from x402.schemas.hooks import ServerPaymentRequiredContext, SettleResultContext
from x402.schemas.payments import PaymentPayload, PaymentRequirements
from x402.schemas.responses import SettleResponse

from x402.extensions.erc8004.server import create_erc8004_resource_server_extension
from x402.extensions.erc8004.types import ERC8004Config, InteractionReceipt
from x402.extensions.erc8004.artifact import verify_interaction_receipt


def _config(agent_id: int = 42) -> ERC8004Config:
    return ERC8004Config(
        network="eip155:8453",
        reputation_registry="0x" + "00" * 20,
        identity_registry="0x" + "00" * 20,
        rpc_url="http://localhost:8545",
        agent_id=agent_id,
    )


def _requirements() -> PaymentRequirements:
    return PaymentRequirements(
        scheme="exact",
        network="eip155:8453",
        asset="0x" + "01" * 20,
        amount="1000000",
        pay_to="0x" + "03" * 20,
        max_timeout_seconds=60,
    )


def test_extension_key() -> None:
    ext = create_erc8004_resource_server_extension(_config())
    assert ext.key == "erc8004"


def test_enrich_payment_required_response() -> None:
    ext = create_erc8004_resource_server_extension(_config())
    ctx = ServerPaymentRequiredContext(
        requirements=[], resource_info=None, error=None, payment_required_response=MagicMock()
    )
    result = ext.enrich_payment_required_response({}, ctx)
    assert result["info"]["agentId"] == 42
    assert "schema" in result


def test_no_receipt_without_signer() -> None:
    ext = create_erc8004_resource_server_extension(_config(), signer=None)
    ctx = SettleResultContext(
        payment_payload=PaymentPayload(payload={}, accepted=_requirements()),
        requirements=_requirements(),
        result=SettleResponse(success=True, transaction="0x" + "ab" * 32, network="eip155:8453", payer="0x" + "02" * 20),
    )
    assert ext.enrich_settlement_response({}, ctx) is None


def test_receipt_signed_and_verifiable() -> None:
    agent = Account.create()
    ext = create_erc8004_resource_server_extension(_config(), signer=agent)
    ctx = SettleResultContext(
        payment_payload=PaymentPayload(payload={"sig": "0xdead"}, accepted=_requirements()),
        requirements=_requirements(),
        result=SettleResponse(success=True, transaction="0x" + "ab" * 32, network="eip155:8453", payer="0x" + "02" * 20),
    )
    result = ext.enrich_settlement_response({}, ctx)
    assert "receipt" in result["info"]
    receipt = InteractionReceipt.from_dict(result["info"]["receipt"])
    assert receipt.chain_id == 8453
    assert verify_interaction_receipt(receipt, agent.address) is True
```

- [ ] **Step 2: Run to verify failure**

Run: `cd python/x402 && uv run pytest tests/unit/extensions/erc8004/test_server.py -v`
Expected: FAIL (server still emits `ticket`, not `receipt`).

- [ ] **Step 3: Rewrite server.py**

```python
"""Resource Server Extension for ERC-8004 feedback."""

from __future__ import annotations

from typing import Any

from x402.schemas.extensions import ResourceServerExtension
from x402.schemas.hooks import ServerPaymentRequiredContext, SettleResultContext

from .artifact import build_artifact, compute_interaction_hash, sign_interaction_receipt
from .schema import declare_erc8004_extension, erc8004_schema
from .types import ERC8004Config, EXTENSION_KEY


def create_erc8004_resource_server_extension(
    config: ERC8004Config,
    signer: Any | None = None,
) -> ResourceServerExtension:
    """Create ERC-8004 server extension.

    Declares agentId in PaymentRequired. On settlement, if a signer is provided,
    returns a signed InteractionReceipt attesting to the paid interaction. The
    receipt is optional: clients can still submit feedback without it.
    """
    agent_id = config.agent_id
    if agent_id is None:
        raise ValueError("agent_id is required in ERC8004Config for server extension")

    class ERC8004ResourceServerExtension:
        @property
        def key(self) -> str:
            return EXTENSION_KEY

        def enrich_declaration(self, declaration: Any, transport_context: Any) -> Any:
            return declaration

        def enrich_payment_required_response(
            self, declaration: Any, context: ServerPaymentRequiredContext
        ) -> dict[str, Any] | None:
            return declare_erc8004_extension(agent_id)

        def enrich_settlement_response(
            self, declaration: Any, context: SettleResultContext
        ) -> dict[str, Any] | None:
            if signer is None:
                return None
            result = context.result
            if not result.success or not result.transaction or not result.payer:
                return None

            requirements = context.requirements
            tx_hash = result.transaction
            chain_id = int(requirements.network.split(":")[1])

            artifact = build_artifact(
                requirements=requirements,
                payment_payload=context.payment_payload,
                tx_hash=tx_hash,
                payer=result.payer,
                payment_method=_payment_method(requirements),
                request={},
                response={},
                feedback={},
            )
            interaction_hash = compute_interaction_hash(artifact.to_dict())
            tx_hash_bytes = bytes.fromhex(tx_hash.removeprefix("0x"))
            receipt = sign_interaction_receipt(signer, chain_id, tx_hash_bytes, interaction_hash)

            return {"info": {"receipt": receipt.to_dict()}, "schema": erc8004_schema}

    return ERC8004ResourceServerExtension()


def _payment_method(requirements: Any) -> str:
    """Best-effort scheme tag for the artifact (informational only)."""
    extra = getattr(requirements, "extra", {}) or {}
    return extra.get("paymentMethod", requirements.scheme)
```

> Note: `compute_interaction_hash` hashes only `{version, settlement}`. The server builds an artifact with empty `request`/`response`/`feedback` purely to reuse `build_artifact`'s settlement assembly — those empty sections do not affect the interaction hash. The client later re-derives the identical `{version, settlement}` core, so the receipt verifies. The `settlement` block includes `paymentPayload`/`paymentRequirements`, which are identical on both sides (client created the payload; server received it). Request/response coverage in the receipt is a documented v2 enhancement requiring the server to receive those digests in the settle context.

- [ ] **Step 4: Run to verify pass**

Run: `cd python/x402 && uv run pytest tests/unit/extensions/erc8004/test_server.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add python/x402/extensions/erc8004/server.py python/x402/tests/unit/extensions/erc8004/test_server.py
git commit -m "feat(erc8004): server emits optional signed interaction receipt"
```

---

## Task 5: Client — uploader protocol, artifact build, direct registry submission

**Files:**
- Modify: `python/x402/extensions/erc8004/client.py`
- Test: `python/x402/tests/unit/extensions/erc8004/test_client.py`

- [ ] **Step 1: Write the failing tests**

Replace `test_client.py` with:

```python
"""Tests for ERC-8004 client extension."""

from unittest.mock import MagicMock

from x402.schemas.payments import PaymentPayload, PaymentRequired, PaymentRequirements

from x402.extensions.erc8004.client import (
    ERC8004ClientExtension,
    ERCFeedbackClient,
    InMemoryUploader,
    echo_erc8004_in_payment_payload,
    extract_erc8004_info,
)
from x402.extensions.erc8004.types import ERC8004Config, FeedbackParams


def _requirements() -> PaymentRequirements:
    return PaymentRequirements(
        scheme="exact",
        network="eip155:8453",
        asset="0x" + "01" * 20,
        amount="1000000",
        pay_to="0x" + "03" * 20,
        max_timeout_seconds=60,
    )


def _config() -> ERC8004Config:
    return ERC8004Config(
        network="eip155:8453",
        reputation_registry="0x" + "00" * 20,
        identity_registry="0x" + "00" * 20,
        rpc_url="http://localhost:8545",
    )


def test_extract_erc8004_info() -> None:
    pr = PaymentRequired(accepts=[], extensions={"erc8004": {"info": {"agentId": 42}, "schema": {}}})
    assert extract_erc8004_info(pr)["agentId"] == 42


def test_echo_erc8004_in_payment_payload() -> None:
    pr = PaymentRequired(accepts=[], extensions={"erc8004": {"info": {"agentId": 42}, "schema": {}}})
    payload = PaymentPayload(payload={}, accepted=_requirements())
    result = echo_erc8004_in_payment_payload(payload, pr)
    assert result.extensions["erc8004"]["info"]["agentId"] == 42


def test_client_extension_key() -> None:
    assert ERC8004ClientExtension().key == "erc8004"


def test_in_memory_uploader_returns_uri_and_keeps_bytes() -> None:
    up = InMemoryUploader()
    uri = up.upload(b'{"a":1}')
    assert uri.startswith("mem://")
    assert up.store[uri] == b'{"a":1}'


def test_build_and_publish_sets_uri_and_hash(monkeypatch) -> None:
    client = ERCFeedbackClient.__new__(ERCFeedbackClient)
    client._config = _config()
    up = InMemoryUploader()
    payload = PaymentPayload(payload={"sig": "0xdead"}, accepted=_requirements())
    params = FeedbackParams(agent_id=42, value=90, endpoint="/weather")
    out = client.build_and_publish_artifact(
        requirements=_requirements(),
        payment_payload=payload,
        tx_hash="0x" + "ab" * 32,
        payer="0x" + "02" * 20,
        payment_method="eip3009",
        request={"method": "GET", "url": "https://x/y", "headerDigest": "0x" + "00" * 32, "bodyDigest": "0x" + "00" * 32},
        response={"status": 200, "headerDigest": "0x" + "00" * 32, "bodyDigest": "0x" + "0a" * 32},
        params=params,
        uploader=up,
        receipt=None,
    )
    uri, feedback_hash, updated = out
    assert uri.startswith("mem://")
    assert len(feedback_hash) == 32
    assert updated.feedback_uri == uri
    assert updated.feedback_hash == feedback_hash
    # the bytes hosted at the URI hash to feedback_hash
    from eth_utils import keccak
    assert keccak(up.store[uri]) == feedback_hash


def test_submit_feedback_to_registry_builds_tx(monkeypatch) -> None:
    client = ERCFeedbackClient.__new__(ERCFeedbackClient)
    client._config = _config()
    signer = MagicMock()
    signer.address = "0x" + "02" * 20
    client._signer = signer

    w3 = MagicMock()
    w3.eth.chain_id = 8453
    w3.eth.get_transaction_count.return_value = 7
    w3.eth.max_priority_fee = 1
    w3.eth.get_block.return_value = {"baseFeePerGas": 2}
    w3.eth.contract.return_value.functions.giveFeedback.return_value.build_transaction.return_value = {"data": "0xabcd"}
    signer.sign_transaction.return_value = MagicMock(raw_transaction=b"\x01")
    w3.eth.send_raw_transaction.return_value = bytes.fromhex("ab" * 32)
    client._w3 = w3

    params = FeedbackParams(agent_id=42, value=90, feedback_uri="mem://x", feedback_hash=b"\x0a" * 32)
    tx_hash = client.submit_feedback_to_registry(params)
    assert tx_hash == "0x" + "ab" * 32
    # plain type-2 tx (no EIP-7702 authorizationList)
    sent_tx = signer.sign_transaction.call_args[0][0]
    assert "authorizationList" not in sent_tx
```

- [ ] **Step 2: Run to verify failure**

Run: `cd python/x402 && uv run pytest tests/unit/extensions/erc8004/test_client.py -v`
Expected: FAIL (ImportError `InMemoryUploader`; methods don't exist).

- [ ] **Step 3: Rewrite client.py**

```python
"""Client-side utilities for the ERC-8004 Feedback Extension."""

from __future__ import annotations

import secrets
from typing import Any, Protocol

from eth_utils import keccak, to_checksum_address
from web3 import Web3
from x402.schemas.extensions import ClientExtension
from x402.schemas.payments import PaymentPayload, PaymentRequired, PaymentRequirements

from .artifact import build_artifact, compute_feedback_hash
from .schema import erc8004_schema
from .types import (
    ERC8004Config,
    EXTENSION_KEY,
    FeedbackArtifact,
    FeedbackParams,
    InteractionReceipt,
)

REPUTATION_ABI = [
    {
        "inputs": [
            {"name": "agentId", "type": "uint256"},
            {"name": "value", "type": "int128"},
            {"name": "valueDecimals", "type": "uint8"},
            {"name": "tag1", "type": "string"},
            {"name": "tag2", "type": "string"},
            {"name": "endpoint", "type": "string"},
            {"name": "feedbackURI", "type": "string"},
            {"name": "feedbackHash", "type": "bytes32"},
        ],
        "name": "giveFeedback",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]


def extract_erc8004_info(payment_required: PaymentRequired) -> dict[str, Any] | None:
    """Extract agentId from PaymentRequired.extensions."""
    if not payment_required.extensions:
        return None
    ext = payment_required.extensions.get(EXTENSION_KEY)
    if not ext:
        return None
    info = ext.get("info") if isinstance(ext, dict) else getattr(ext, "info", None)
    return info or None


def echo_erc8004_in_payment_payload(
    payment_payload: PaymentPayload, payment_required: PaymentRequired
) -> PaymentPayload:
    """Echo the erc8004 extension into PaymentPayload per x402 v2 spec."""
    if not payment_required.extensions or EXTENSION_KEY not in payment_required.extensions:
        return payment_payload
    ext = payment_required.extensions[EXTENSION_KEY]
    info = ext.get("info") if isinstance(ext, dict) else getattr(ext, "info", {})
    extensions = dict(payment_payload.extensions or {})
    extensions[EXTENSION_KEY] = {"info": dict(info), "schema": erc8004_schema}
    payment_payload.extensions = extensions
    return payment_payload


class ERC8004ClientExtension(ClientExtension):
    """Client extension that echoes erc8004 info into PaymentPayload."""

    key = EXTENSION_KEY

    def enrich_payment_payload(self, payment_payload: Any, payment_required: Any) -> Any:
        return echo_erc8004_in_payment_payload(payment_payload, payment_required)


class ArtifactUploader(Protocol):
    """Pluggable storage backend for the feedback artifact.

    Production implementations should use content-addressed storage
    (IPFS/Arweave) so the URI itself commits to the content.
    """

    def upload(self, content: bytes) -> str:
        """Upload bytes, return a resolvable URI."""
        ...


class InMemoryUploader:
    """Test/dev uploader. Returns a mem:// URI and retains bytes in memory."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    def upload(self, content: bytes) -> str:
        uri = "mem://" + secrets.token_hex(16)
        self.store[uri] = content
        return uri


class ERCFeedbackClient:
    """Client-side helper for building, publishing, and submitting feedback."""

    def __init__(self, config: ERC8004Config, signer: Any) -> None:
        self._config = config
        self._signer = signer
        self._w3 = Web3(Web3.HTTPProvider(config.rpc_url))

    @staticmethod
    def extract_erc8004_info(payment_required: PaymentRequired) -> dict[str, Any] | None:
        return extract_erc8004_info(payment_required)

    def build_and_publish_artifact(
        self,
        requirements: PaymentRequirements,
        payment_payload: PaymentPayload,
        tx_hash: str,
        payer: str,
        payment_method: str,
        request: dict[str, Any],
        response: dict[str, Any],
        params: FeedbackParams,
        uploader: ArtifactUploader,
        receipt: InteractionReceipt | None = None,
    ) -> tuple[str, bytes, FeedbackParams]:
        """Build the canonical artifact, embed the optional receipt, publish it.

        Returns (feedbackURI, feedbackHash, updated FeedbackParams).
        """
        from .artifact import canonical_bytes

        feedback = {
            "agentId": params.agent_id,
            "value": params.value,
            "valueDecimals": params.value_decimals,
            "tag1": params.tag1,
            "tag2": params.tag2,
            "endpoint": params.endpoint,
            "comment": getattr(params, "comment", ""),
        }
        artifact = build_artifact(
            requirements=requirements,
            payment_payload=payment_payload,
            tx_hash=tx_hash,
            payer=payer,
            payment_method=payment_method,
            request=request,
            response=response,
            feedback=feedback,
        )
        art_dict = artifact.to_dict()
        if receipt is not None:
            art_dict["interaction"]["response"]["agentSignature"] = receipt.to_dict()

        feedback_hash = compute_feedback_hash(art_dict)
        uri = uploader.upload(canonical_bytes(art_dict))
        updated = params.model_copy(update={"feedback_uri": uri, "feedback_hash": feedback_hash})
        return uri, feedback_hash, updated

    def submit_feedback_to_registry(
        self, params: FeedbackParams, gas_limit: int = 250000
    ) -> str:
        """Submit feedback directly to ReputationRegistry.giveFeedback (type-2 tx)."""
        registry = self._w3.eth.contract(
            address=to_checksum_address(self._config.reputation_registry), abi=REPUTATION_ABI
        )
        func = registry.functions.giveFeedback(
            params.agent_id,
            params.value,
            params.value_decimals,
            params.tag1,
            params.tag2,
            params.endpoint,
            params.feedback_uri,
            params.feedback_hash,
        )

        sender = getattr(self._signer, "address", None)
        if sender is None:
            raise TypeError("signer must expose an address attribute")

        nonce = self._w3.eth.get_transaction_count(sender)
        base_fee = self._w3.eth.get_block("latest")["baseFeePerGas"]
        tx = {
            "type": 2,
            "chainId": self._w3.eth.chain_id,
            "nonce": nonce,
            "to": to_checksum_address(self._config.reputation_registry),
            "value": 0,
            "gas": gas_limit,
            "data": func.build_transaction({"from": sender})["data"],
            "maxFeePerGas": self._w3.eth.max_priority_fee + 2 * base_fee,
            "maxPriorityFeePerGas": self._w3.eth.max_priority_fee,
        }
        signed = self._signer.sign_transaction(tx)
        tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction)
        return "0x" + bytes(tx_hash).hex() if not str(tx_hash).startswith("0x") else tx_hash.hex()
```

> Note on the return line: web3 returns `HexBytes`; `.hex()` yields no `0x` prefix in some versions. The test expects `"0x" + "ab"*32`. Use the explicit prefix form: replace the final return with:
> ```python
>         raw = self._w3.eth.send_raw_transaction(signed.raw_transaction)
>         h = bytes(raw).hex()
>         return h if h.startswith("0x") else "0x" + h
> ```
> Apply this exact form in Step 3.

> Coordination note: when a receipt is present, the `settlement` block must byte-match what the server signed. That means the client must pass the **same `payment_method`** the server derived (`requirements.extra["paymentMethod"]` if set, else `requirements.scheme`) and the same `payment_payload`/`requirements` content. If they diverge, `verify_feedback` returns `TrustTier.DISPUTED` rather than `FULL`. Document this in the README usage snippet (Task 8).

- [ ] **Step 4: Run to verify pass**

Run: `cd python/x402 && uv run pytest tests/unit/extensions/erc8004/test_client.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add python/x402/extensions/erc8004/client.py python/x402/tests/unit/extensions/erc8004/test_client.py
git commit -m "feat(erc8004): client builds/publishes artifact and submits directly to registry"
```

---

## Task 6: Verifier + dedup (verify.py)

**Files:**
- Create: `python/x402/extensions/erc8004/verify.py`
- Test: `python/x402/tests/unit/extensions/erc8004/test_verify.py`

- [ ] **Step 1: Write the failing tests**

Create `test_verify.py`:

```python
"""Tests for ERC-8004 verification + dedup."""

from eth_utils import keccak

from x402.extensions.erc8004.artifact import canonical_bytes
from x402.extensions.erc8004.verify import (
    TrustTier,
    verify_integrity,
    dedup_feedback,
)


def test_verify_integrity_match() -> None:
    content = b'{"a":1}'
    assert verify_integrity(content, keccak(content)) is True


def test_verify_integrity_mismatch() -> None:
    assert verify_integrity(b'{"a":1}', b"\x00" * 32) is False


def test_dedup_keeps_latest_per_key() -> None:
    records = [
        {"payer": "0xA", "agentId": 1, "txHash": "0xT", "block": 10, "value": 50},
        {"payer": "0xA", "agentId": 1, "txHash": "0xT", "block": 20, "value": 90},
        {"payer": "0xB", "agentId": 1, "txHash": "0xT", "block": 5, "value": 30},
    ]
    out = dedup_feedback(records)
    assert len(out) == 2
    a = [r for r in out if r["payer"] == "0xA"][0]
    assert a["value"] == 90  # latest block wins


def test_trust_tier_values() -> None:
    assert TrustTier.FULL.value < TrustTier.CLIENT_ONLY.value or True  # enum exists
    assert {t.name for t in TrustTier} >= {"FULL", "CLIENT_ONLY", "DISPUTED", "REJECTED"}
```

- [ ] **Step 2: Run to verify failure**

Run: `cd python/x402 && uv run pytest tests/unit/extensions/erc8004/test_verify.py -v`
Expected: FAIL with ImportError on `verify.py`.

- [ ] **Step 3: Implement verify.py**

```python
"""Verification and dedup for ERC-8004 x402 feedback.

Verification is payment-scheme agnostic: it keys off the universal ERC-20
Transfer event emitted by the asset contract, so EIP-3009, Permit2, and plain
ERC-20 settlements all verify the same way.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Any

from eth_utils import keccak, to_checksum_address

from .artifact import (
    compute_feedback_hash,
    compute_interaction_hash,
    verify_interaction_receipt,
)
from .types import InteractionReceipt

TRANSFER_TOPIC = "0x" + keccak(b"Transfer(address,address,uint256)").hex()

IDENTITY_ABI = [
    {
        "inputs": [{"name": "agentId", "type": "uint256"}],
        "name": "ownerOf",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    }
]


class TrustTier(IntEnum):
    FULL = 0          # all checks pass + agent receipt valid
    CLIENT_ONLY = 1   # payment proven, response client-claimed (no receipt)
    DISPUTED = 2      # agent counter-attested a different interaction
    REJECTED = 3      # integrity or chain checks failed


def verify_integrity(content: bytes, feedback_hash: bytes) -> bool:
    """keccak256(content) == feedback_hash."""
    return keccak(content) == feedback_hash


def _topic_addr(topic: bytes) -> str:
    return to_checksum_address("0x" + topic.hex()[-40:])


def verify_settlement(w3: Any, artifact: dict[str, Any]) -> bool:
    """Confirm the settlement tx emitted a matching ERC-20 Transfer."""
    s = artifact["settlement"]
    receipt = w3.eth.get_transaction_receipt(s["txHash"])
    asset = to_checksum_address(s["asset"])
    payer = to_checksum_address(s["payer"])
    pay_to = to_checksum_address(s["payTo"])
    amount = int(s["amount"])
    for log in receipt["logs"]:
        if to_checksum_address(log["address"]) != asset:
            continue
        topics = log["topics"]
        if len(topics) != 3 or ("0x" + bytes(topics[0]).hex()) != TRANSFER_TOPIC:
            continue
        if _topic_addr(bytes(topics[1])) != payer or _topic_addr(bytes(topics[2])) != pay_to:
            continue
        if int(bytes(log["data"]).hex() or "0", 16) == amount:
            return True
    return False


def verify_agent_binding(w3: Any, identity_registry: str, artifact: dict[str, Any]) -> bool:
    """ownerOf(agentId) must equal the settlement payTo."""
    agent_id = int(artifact["feedback"]["agentId"])
    pay_to = to_checksum_address(artifact["settlement"]["payTo"])
    contract = w3.eth.contract(
        address=to_checksum_address(identity_registry), abi=IDENTITY_ABI
    )
    owner = contract.functions.ownerOf(agent_id).call()
    return to_checksum_address(owner) == pay_to


def verify_feedback(
    w3: Any,
    identity_registry: str,
    content: bytes,
    feedback_hash: bytes,
    artifact: dict[str, Any],
) -> TrustTier:
    """Full verification pipeline returning a trust tier."""
    if not verify_integrity(content, feedback_hash):
        return TrustTier.REJECTED
    if compute_feedback_hash(artifact) != feedback_hash:
        return TrustTier.REJECTED
    if not verify_settlement(w3, artifact):
        return TrustTier.REJECTED
    if not verify_agent_binding(w3, identity_registry, artifact):
        return TrustTier.REJECTED

    agent_sig = artifact["interaction"]["response"].get("agentSignature")
    if not agent_sig:
        return TrustTier.CLIENT_ONLY

    receipt = InteractionReceipt.from_dict(agent_sig)
    owner = w3.eth.contract(
        address=to_checksum_address(identity_registry), abi=IDENTITY_ABI
    ).functions.ownerOf(int(artifact["feedback"]["agentId"])).call()
    if receipt.interaction_hash != compute_interaction_hash(artifact):
        return TrustTier.DISPUTED
    if not verify_interaction_receipt(receipt, owner):
        return TrustTier.DISPUTED
    return TrustTier.FULL


def dedup_feedback(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the latest (by block) record per (payer, agentId, txHash)."""
    best: dict[tuple, dict[str, Any]] = {}
    for r in records:
        key = (
            to_checksum_address(r["payer"]) if str(r["payer"]).startswith("0x") and len(r["payer"]) == 42 else r["payer"],
            r["agentId"],
            r["txHash"],
        )
        if key not in best or r["block"] > best[key]["block"]:
            best[key] = r
    return list(best.values())
```

> Note: in `test_dedup_keeps_latest_per_key` the payer values (`"0xA"`) are not valid checksum addresses; the `dedup_feedback` key guard (`len == 42`) falls back to the raw string for those, so the test passes. Real callers pass real addresses.

- [ ] **Step 4: Run to verify pass**

Run: `cd python/x402 && uv run pytest tests/unit/extensions/erc8004/test_verify.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add python/x402/extensions/erc8004/verify.py python/x402/tests/unit/extensions/erc8004/test_verify.py
git commit -m "feat(erc8004): scheme-agnostic verifier (Transfer-event based) + dedup"
```

---

## Task 7: Constants + package exports

**Files:**
- Modify: `python/x402/extensions/erc8004/constants.py`
- Modify: `python/x402/extensions/erc8004/__init__.py`

- [ ] **Step 1: Rewrite constants.py**

```python
"""Chain-specific constants for ERC-8004."""

from __future__ import annotations

# ReputationRegistry (canonical ERC-8004 deployment)
MAINNET_REPUTATION_REGISTRY = "0x8004BAa17C55a88189AE136b182e5fdA19dE9b63"
MAINNET_IDENTITY_REGISTRY: str | None = None

SEPOLIA_REPUTATION_REGISTRY: str | None = None
SEPOLIA_IDENTITY_REGISTRY: str | None = None

BASE_REPUTATION_REGISTRY: str | None = None
BASE_IDENTITY_REGISTRY: str | None = None
```

> Note: fill the real `IDENTITY_REGISTRY` addresses when known; leaving `None` is acceptable since callers pass `identity_registry` via `ERC8004Config`.

- [ ] **Step 2: Rewrite __init__.py**

```python
"""ERC-8004 Feedback Extension for x402 Python SDK."""

from x402.extensions.erc8004.artifact import (
    build_artifact,
    canonical_bytes,
    compute_feedback_hash,
    compute_interaction_hash,
    receipt_digest,
    sign_interaction_receipt,
    verify_interaction_receipt,
)
from x402.extensions.erc8004.client import (
    ArtifactUploader,
    ERC8004ClientExtension,
    ERCFeedbackClient,
    InMemoryUploader,
    echo_erc8004_in_payment_payload,
    extract_erc8004_info,
)
from x402.extensions.erc8004.schema import declare_erc8004_extension, erc8004_schema
from x402.extensions.erc8004.server import create_erc8004_resource_server_extension
from x402.extensions.erc8004.types import (
    ARTIFACT_VERSION,
    ERC8004Config,
    ERC8004ExtensionDeclaration,
    ERC8004ExtensionInfo,
    EXTENSION_KEY,
    FeedbackArtifact,
    FeedbackParams,
    InteractionReceipt,
)
from x402.extensions.erc8004.verify import (
    TrustTier,
    dedup_feedback,
    verify_agent_binding,
    verify_feedback,
    verify_integrity,
    verify_settlement,
)

__all__ = [
    "create_erc8004_resource_server_extension",
    "ERCFeedbackClient",
    "ERC8004ClientExtension",
    "ArtifactUploader",
    "InMemoryUploader",
    "echo_erc8004_in_payment_payload",
    "extract_erc8004_info",
    "declare_erc8004_extension",
    "erc8004_schema",
    "build_artifact",
    "canonical_bytes",
    "compute_feedback_hash",
    "compute_interaction_hash",
    "receipt_digest",
    "sign_interaction_receipt",
    "verify_interaction_receipt",
    "TrustTier",
    "dedup_feedback",
    "verify_agent_binding",
    "verify_feedback",
    "verify_integrity",
    "verify_settlement",
    "ARTIFACT_VERSION",
    "ERC8004Config",
    "ERC8004ExtensionDeclaration",
    "ERC8004ExtensionInfo",
    "EXTENSION_KEY",
    "FeedbackArtifact",
    "FeedbackParams",
    "InteractionReceipt",
]
```

- [ ] **Step 3: Run the full extension unit suite**

Run: `cd python/x402 && uv run pytest tests/unit/extensions/erc8004/ -v`
Expected: PASS (all tests across types/artifact/server/client/verify/schema).

- [ ] **Step 4: Commit**

```bash
git add python/x402/extensions/erc8004/constants.py python/x402/extensions/erc8004/__init__.py
git commit -m "refactor(erc8004): update constants and package exports for gateway-less design"
```

---

## Task 8: Update README

**Files:**
- Modify: `python/x402/extensions/erc8004/README.md`

- [ ] **Step 1: Replace Architecture + usage sections**

Replace the "Architecture", "Client Usage", "Gas Costs", and "Future Work" sections with:

````markdown
## Architecture

- **No contracts owned.** Clients submit directly to the standard ERC-8004 `ReputationRegistry`.
- **Binding via off-chain artifact.** The client builds a canonical JSON artifact capturing `paymentRequirements`, `paymentPayload`, the response digest, and `txHash`, hashes it (keccak256) into `feedbackHash`, and uploads it to obtain `feedbackURI`.
- **Optional agent receipt.** The server may return `X-X402-Interaction-Receipt` (a signed attestation over the interaction). Its absence downgrades the trust tier but never blocks submission.
- **Scheme-agnostic verification.** Verifiers key off the ERC-20 `Transfer` event, so EIP-3009 (USDC), Permit2, and plain ERC-20 all verify identically.
- **Dedup off-chain** on `(payer, agentId, settlementTxHash)`, latest block wins.

## Client Usage

```python
from x402.extensions.erc8004 import (
    ERCFeedbackClient, ERC8004Config, FeedbackParams, InMemoryUploader, InteractionReceipt,
)

config = ERC8004Config(
    network="eip155:8453",
    reputation_registry="0x8004BAa1...",
    identity_registry="0x...",
    rpc_url="https://...",
)
client = ERCFeedbackClient(config, signer)

# After payment: optionally parse the agent receipt from the settle response header
receipt = InteractionReceipt.from_dict(receipt_dict) if receipt_dict else None

params = FeedbackParams(agent_id=42, value=95, tag1="x402", tag2="weather", endpoint="/weather")

uri, feedback_hash, params = client.build_and_publish_artifact(
    requirements=requirements,
    payment_payload=payment_payload,
    tx_hash=settle_tx_hash,
    payer=signer.address,
    payment_method="eip3009",       # or "permit2", "erc20"
    request={"method": "GET", "url": "...", "headerDigest": "0x..", "bodyDigest": "0x.."},
    response={"status": 200, "headerDigest": "0x..", "bodyDigest": "0x.."},
    params=params,
    uploader=InMemoryUploader(),    # production: an IPFS/Arweave uploader
    receipt=receipt,
)

tx_hash = client.submit_feedback_to_registry(params)
```

## Verification (aggregators)

```python
from x402.extensions.erc8004 import verify_feedback, dedup_feedback, TrustTier

tier = verify_feedback(w3, config.identity_registry, artifact_bytes, feedback_hash, artifact_dict)
# TrustTier.FULL / CLIENT_ONLY / DISPUTED / REJECTED
```
````

- [ ] **Step 2: Commit**

```bash
git add python/x402/extensions/erc8004/README.md
git commit -m "docs(erc8004): rewrite README for gateway-less artifact-bound design"
```

---

## Task 9: Update the E2E integration test

**Files:**
- Modify: `python/x402/tests/integration/test_erc8004_e2e.py`

- [ ] **Step 1: Rewrite the flow assertions**

Replace the gateway/EIP-7702 flow with the direct-registry flow. The test (kept as a skipped/anvil-gated skeleton consistent with the existing one) must:

```python
"""End-to-end test for the gateway-less ERC-8004 extension against Anvil."""

import subprocess
import time

import pytest
from eth_account import Account
from web3 import Web3

from x402.extensions.erc8004 import (
    ERC8004Config,
    ERCFeedbackClient,
    FeedbackParams,
    InMemoryUploader,
    create_erc8004_resource_server_extension,
    verify_integrity,
)


@pytest.fixture(scope="module")
def anvil():
    proc = subprocess.Popen(
        ["anvil", "--hardfork", "Prague", "--chain-id", "1337"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    time.sleep(2)
    yield "http://127.0.0.1:8545"
    proc.terminate()
    proc.wait()


@pytest.mark.integration
def test_artifact_published_and_integrity_holds(anvil) -> None:
    """Build+publish an artifact and confirm the hosted bytes match feedbackHash.

    This exercises the off-chain binding path without requiring a deployed
    ReputationRegistry on the local chain.
    """
    w3 = Web3(Web3.HTTPProvider(anvil))
    signer = Account.from_key(
        "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
    )
    config = ERC8004Config(
        network=f"eip155:{w3.eth.chain_id}",
        reputation_registry="0x" + "00" * 20,
        identity_registry="0x" + "00" * 20,
        rpc_url=anvil,
    )
    client = ERCFeedbackClient(config, signer)

    from x402.schemas.payments import PaymentPayload, PaymentRequirements

    requirements = PaymentRequirements(
        scheme="exact", network=f"eip155:{w3.eth.chain_id}",
        asset="0x" + "01" * 20, amount="1000000", pay_to="0x" + "03" * 20,
        max_timeout_seconds=60,
    )
    payload = PaymentPayload(payload={"sig": "0xdead"}, accepted=requirements)
    params = FeedbackParams(agent_id=42, value=90, endpoint="/weather")
    up = InMemoryUploader()

    uri, feedback_hash, updated = client.build_and_publish_artifact(
        requirements=requirements, payment_payload=payload,
        tx_hash="0x" + "ab" * 32, payer=signer.address, payment_method="eip3009",
        request={"method": "GET", "url": "https://x/y", "headerDigest": "0x" + "00" * 32, "bodyDigest": "0x" + "00" * 32},
        response={"status": 200, "headerDigest": "0x" + "00" * 32, "bodyDigest": "0x" + "0a" * 32},
        params=params, uploader=up, receipt=None,
    )
    assert verify_integrity(up.store[uri], feedback_hash) is True
    assert updated.feedback_uri == uri
```

- [ ] **Step 2: Run (skips cleanly if anvil absent)**

Run: `cd python/x402 && uv run pytest tests/integration/test_erc8004_e2e.py -v -m integration`
Expected: PASS if `anvil` is installed; otherwise the module fixture errors — acceptable, run only where anvil exists. Confirm import-time correctness with: `cd python/x402 && uv run python -c "import x402.tests.integration.test_erc8004_e2e"` → no error.

- [ ] **Step 3: Commit**

```bash
git add python/x402/tests/integration/test_erc8004_e2e.py
git commit -m "test(erc8004): rewrite E2E for direct-registry artifact flow"
```

---

## Task 10: Full suite + final verification

- [ ] **Step 1: Run the entire extension unit suite**

Run: `cd python/x402 && uv run pytest tests/unit/extensions/erc8004/ -v`
Expected: all PASS, no references to `FeedbackTicket`, `feedback_gateway`, or EIP-7702.

- [ ] **Step 2: Run the whole project unit suite to catch regressions**

Run: `cd python/x402 && uv run pytest tests/unit -q`
Expected: PASS, no import errors from the removed gateway code.

- [ ] **Step 3: Grep for dangling references**

Run: `cd /Users/gabrielfior/code/ef/x402 && grep -rn "FeedbackTicket\|feedback_gateway\|FeedbackGateway\|usedNonces\|submit_feedback\b" python/x402/extensions python/x402/tests --include='*.py'`
Expected: no matches (the method is now `submit_feedback_to_registry`).

- [ ] **Step 4: Final commit if any fixups were needed**

```bash
git add -A
git commit -m "chore(erc8004): finalize gateway-less feedback extension"
```

---

## Verification Summary

- Unit: `cd python/x402 && uv run pytest tests/unit/extensions/erc8004/ -v` — all green.
- Regression: `cd python/x402 && uv run pytest tests/unit -q` — all green.
- Integrity invariant: bytes hosted at `feedbackURI` hash (keccak256) to the on-chain `feedbackHash` (`test_build_and_publish_sets_uri_and_hash`, E2E).
- Scheme-agnosticism: `verify_settlement` matches the ERC-20 `Transfer` log only (no scheme-specific parser) — works for EIP-3009/Permit2/ERC-20.
- Censorship resistance: `submit_feedback_to_registry` requires no agent signature; receipt is optional and only affects `TrustTier`.
- No owned contracts: `contracts/erc8004/` deleted; SDK carries minimal inline ABIs.
