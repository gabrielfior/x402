"""Run the ERC-8004 feedback flow against a LOCAL Anvil fork of your RPC.

Same end-to-end flow as `run_on_fork.py`, but instead of hitting your RPC
directly it forks it into a local Anvil subprocess (so the already-deployed
ERC-8004 registries / USDC / DAI / Permit2 proxy are all present) and funds the
PAYER automatically:

  - ETH: minted directly via `anvil_setBalance`,
  - ERC-20 (USDC, and DAI for the Permit2 leg): pulled from a whale via
    `anvil_impersonateAccount` + a transfer to the payer (see
    `utils.fund_erc20_from_whale`), only when the payer's balance is too low.

So you don't need to pre-fund anything — you only need an `RPC_URL` to fork
from and a `PINATA_JWT`. The fork is ephemeral; nothing touches the real chain.

Run:

    cd python/x402
    uv pip install -e .            # one-time
    RPC_URL=... IDENTITY_REGISTRY=0x... ASSET=0x... \
    PAYER_PRIVATE_KEY=0x<any-fresh-key> \
    uv run python ../../examples/python/clients/erc8004/run_on_anvil.py

Optional Permit2 leg: add RUN_DAI_PERMIT2_SCENARIO=1.

Whale overrides (defaults are large mainnet holders): ASSET_WHALE, DAI_WHALE.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time

from eth_account import Account
from web3 import Web3

from utils import fund_erc20_from_whale, parse_config, run_erc8004_demo

# Large mainnet holders used to fund the payer on the local fork (override via env).
DEFAULT_USDC_WHALE = "0x55FE002aefF02F77364de339a1292923A15844B8"  # Circle
DEFAULT_DAI_WHALE = "0x40ec5B33f54e0E8A33A975908C5BA1c14e5BbbDf"   # Polygon (Matic) bridge


def main() -> int:
    cfg = parse_config()

    if shutil.which("anvil") is None:
        print("ERROR: `anvil` not found on PATH. Install Foundry: https://book.getfoundry.sh")
        return 1

    port = int(os.getenv("ANVIL_PORT", "8545"))
    local_url = f"http://127.0.0.1:{port}"
    print(f"forking {cfg.rpc_url} into a local Anvil on {local_url} ...")
    proc = subprocess.Popen(
        ["anvil", "--fork-url", cfg.rpc_url, "--port", str(port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        w3 = Web3(Web3.HTTPProvider(local_url))
        for _ in range(100):
            try:
                if w3.is_connected():
                    break
            except Exception:
                pass
            time.sleep(0.1)
        else:
            print("ERROR: Anvil did not come up. stderr:")
            print((proc.stderr.read() if proc.stderr else b"").decode(errors="replace")[:2000])
            return 1

        payer = Account.from_key(cfg.payer_key)
        w3.provider.make_request("anvil_setBalance", [payer.address, hex(w3.to_wei(1000, "ether"))])
        print(f"funded payer {payer.address} with 1000 ETH on the local fork")

        asset_whale = os.getenv("ASSET_WHALE", DEFAULT_USDC_WHALE)
        dai_whale = os.getenv("DAI_WHALE", DEFAULT_DAI_WHALE)

        def fund_token(token_addr: str, needed: int) -> None:
            whale = dai_whale if Web3.to_checksum_address(token_addr) == cfg.dai_asset else asset_whale
            fund_erc20_from_whale(w3, token_addr, payer.address, needed, whale)

        return run_erc8004_demo(w3=w3, cfg=cfg, effective_rpc_url=local_url, fund_token=fund_token)
    finally:
        proc.terminate()
        proc.wait()


if __name__ == "__main__":
    sys.exit(main())
