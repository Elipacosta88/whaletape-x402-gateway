"""Network configuration, read from the environment.

Extracted verbatim (minus the data-plane bits) from the production file
`x402_market_data_api.py` of whaletape.xyz. Every network is opt-in: if the
receiving address for a network is absent from `.env`, that network does not
exist -- it is not registered on the server and it never shows up in the 402.
That rule is deliberate: the 402 must never advertise a network the server
cannot verify.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

import robinhood_chain

load_dotenv()

# ---------------------------------------------------------------- EVM ----
PAY_TO_ADDRESS = os.environ.get("PAY_TO_ADDRESS")
if not PAY_TO_ADDRESS:
    raise SystemExit(
        "PAY_TO_ADDRESS is not set. Copy .env.example to .env and put the PUBLIC "
        "address (0x...) that receives payments. NEVER put a private key or a "
        "seed phrase in that file."
    )
if not (PAY_TO_ADDRESS.startswith("0x") and len(PAY_TO_ADDRESS) == 42):
    raise SystemExit(
        f"PAY_TO_ADDRESS does not look like an EVM address: {PAY_TO_ADDRESS!r}")

# Base. Default is Base Sepolia (testnet) on purpose: an operator who forgets
# to set NETWORK sells on testnet, not mainnet.
NETWORK = os.environ.get("NETWORK", "eip155:84532")

# Solana rides in the SAME `accepts` as Base (no separate gateway): the SDK
# ships the SVM scheme and the CDP facilitator settles Solana MainNet.
PAY_TO_SOLANA = os.environ.get("PAY_TO_SOLANA") or None
SOLANA_NETWORK = os.environ.get("SOLANA_NETWORK", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp")

# Arbitrum One: same EVM address as Base, native Circle USDC, settled by CDP
# with PayAI as a fallback facilitator.
PAY_TO_ARBITRUM = os.environ.get("PAY_TO_ARBITRUM") or None
ARBITRUM_NETWORK = os.environ.get("ARBITRUM_NETWORK", "eip155:42161")

# Robinhood Chain: same EVM address, but the coin is USDG (see
# robinhood_chain.py) and Naven is the facilitator.
PAY_TO_ROBINHOOD = os.environ.get("PAY_TO_ROBINHOOD") or None
ROBINHOOD_NETWORK = robinhood_chain.NETWORK

# ------------------------------------------------------------ Algorand ----
# Algorand cannot share this process: the AVM scheme lives in the `x402-avm`
# distribution, which SHADOWS the `x402` package. In production it runs as a
# separate service and `caixa_algorand.py` splices its 402 option into this
# one. See the "Algorand" section of README.md.
PAY_TO_ALGORAND = os.environ.get("PAY_TO_ALGORAND") or None
ALGORAND_NETWORK = os.environ.get(
    "ALGORAND_NETWORK", "algorand:wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8=")
ALGORAND_USDC_ASA = os.environ.get("ALGORAND_USDC_ASA", "31566704")
ALGORAND_FEE_PAYER = os.environ.get("ALGORAND_FEE_PAYER") or None
ALGORAND_GATEWAY_URL = os.environ.get("ALGORAND_GATEWAY_URL") or None

# ----------------------------------------------------------- facilitators ----
# Explicit primary facilitator, for a deployment with no CDP credentials.
# When set it WINS over CDP. Leave it empty in production.
FACILITATOR_URL = os.environ.get("FACILITATOR_URL", "").strip() or None
CDP_API_KEY_ID = os.environ.get("CDP_API_KEY_ID")
CDP_API_KEY_SECRET = os.environ.get("CDP_API_KEY_SECRET")
FACILITATOR_RESERVA_URL = os.environ.get(
    "FACILITATOR_RESERVA_URL", "https://facilitator.payai.network").strip()

# ------------------------------------------------------------- serving ----
SITE = os.environ.get("SITE", "https://whaletape.xyz")
BIND_HOST = os.environ.get("BIND_HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8402"))

# Which network the client sees FIRST in `accepts`. Most x402 clients pay the
# first offer they know how to pay, so the order decides the network of the
# sale. `X402_REDE_PRIMEIRA=base` restores the original behaviour without a
# code change.
REDE_PRIMEIRA = os.environ.get("X402_REDE_PRIMEIRA", "solana").strip().lower()
