"""Run the ERC-8004 feedback flow against an EXISTING chain (e.g. a Tenderly
mainnet fork) where the ERC-8004 registries are already deployed.

Unlike `main.py` (which spawns Anvil and deploys mock contracts), this script
talks to your RPC directly and assumes the ReputationRegistry / IdentityRegistry
/ ERC-20 asset already exist at the addresses you pass in. The PAYER must already
be funded on that chain (ETH for gas + >= AMOUNT of ASSET). The shared flow lives
in `utils.run_erc8004_demo`; see `run_on_anvil.py` for a self-funding local fork.

Optional second scenario (DAI + Permit2 via x402 `ExactPermit2Proxy`): set
`RUN_DAI_PERMIT2_SCENARIO=1` and fund the payer with DAI. Env knobs: `DAI_ASSET`,
`DAI_PERMIT2_AMOUNT`, `DAI_PERMIT2_MAX_TIMEOUT`.

Run:

    cd python/x402
    uv pip install -e .            # one-time
    RPC_URL=... PAYER_PRIVATE_KEY=... \
    IDENTITY_REGISTRY=0x... ASSET=0x... \
    uv run python ../../examples/python/clients/erc8004/run_on_fork.py
"""

from __future__ import annotations

import sys

from web3 import Web3

from utils import parse_config, run_erc8004_demo


def main() -> int:
    cfg = parse_config()
    w3 = Web3(Web3.HTTPProvider(cfg.rpc_url))
    if not w3.is_connected():
        print(f"ERROR: cannot connect to RPC at {cfg.rpc_url}")
        return 1
    return run_erc8004_demo(w3=w3, cfg=cfg, effective_rpc_url=cfg.rpc_url, fund_token=None)


if __name__ == "__main__":
    sys.exit(main())
