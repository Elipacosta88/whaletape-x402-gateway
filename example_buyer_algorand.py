"""Paying in USDC on Algorand — the client side, which no SDK gives you.

Why this file exists
====================
Every other chain in this repository is payable with the official `x402`
package. Algorand is not, and the failure is silent: `x402` ships signing
mechanisms for `evm`, `svm` and `tvm` (TON) only, so a canonical client that
reads a 402 offering five networks does not *refuse* the Algorand entry — it
skips it and pays in another network. On a route whose `accepts` contains ONLY
Algorand it raises `NoMatchingRequirementsError` instead.

Measured on the live service, 17-18/09/2026: an indexer paid the same route on
Base, Solana, Arbitrum One and Robinhood Chain within 42 hours and never once
paid Algorand, although the catalogue listed the Algorand option on that exact
route. Nothing was broken. The buyer simply had no mechanism to sign with.

The fix is a different distribution: `x402-avm`. It SHADOWS the `x402` package,
so it needs its own virtualenv — installing both in one breaks the other four
chains (see the Algorand section of the README).

    python3 -m venv .venv-algo
    .venv-algo/bin/pip install x402-avm algosdk
    ALGO_PAYER_MNEMONIC="word word ... word" \
      .venv-algo/bin/python example_buyer_algorand.py https://example.com/coverage

Try it first without spending anything
======================================
Run with `--dry-run` and the script generates a throwaway account instead of
reading your mnemonic. It is never funded, so nothing moves — but the envelope
is signed and presented for real, and the facilitator answers with a real
MainNet simulation:

    Transaction simulation failed: transaction <ID>: asset 31566704 missing
    from <address>

That message is the proof the whole path works: the server parsed the
envelope, called the facilitator and the chain replied. A plain 402 never
produces it. Use this to check a deployment before putting money near it.

The `X-Selfcheck` header marks the attempt as diagnostic so it does not land in
the seller's payment metrics; a well-behaved seller honours it, and sending it
costs you nothing either way.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys

import algosdk
from algosdk import mnemonic
from algosdk.atomic_transaction_composer import AccountTransactionSigner

from x402.client import x402Client
from x402.http.clients import x402HttpxClient
from x402.mechanisms.avm.exact import register_exact_avm_client

USDC_MAINNET_ASA = 31566704


class Signer:
    """The `ClientAvmSigner` the scheme expects.

    The protocol hands over raw msgpack and `algosdk` speaks base64, so the
    conversion lives here and nowhere else. Sign ONLY the indexes you are asked
    for and return `None` for the rest: the other slots in the group belong to
    the facilitator (the fee payer), and signing them would be both wrong and
    dangerous.
    """

    def __init__(self, mnemonico: str):
        sk = mnemonic.to_private_key(mnemonico)
        self.address = algosdk.account.address_from_private_key(sk)
        self._signer = AccountTransactionSigner(sk)

    def sign_transactions(self, unsigned_txns: list[bytes],
                          indexes_to_sign: list[int]) -> list[bytes | None]:
        grupo = [algosdk.encoding.msgpack_decode(base64.b64encode(raw).decode())
                 for raw in unsigned_txns]
        assinadas = self._signer.sign_transactions(grupo, list(indexes_to_sign))
        por_indice = dict(zip(indexes_to_sign, assinadas))
        return [base64.b64decode(algosdk.encoding.msgpack_encode(por_indice[i]))
                if i in por_indice else None
                for i in range(len(unsigned_txns))]


def _signer(dry_run: bool) -> Signer:
    if dry_run:
        sk, _ = algosdk.account.generate_account()
        return Signer(mnemonic.from_private_key(sk))
    frase = os.environ.get("ALGO_PAYER_MNEMONIC", "").strip()
    if not frase:
        sys.exit("set ALGO_PAYER_MNEMONIC (25 words), or pass --dry-run")
    return Signer(frase)


def _receipt(header: str | None) -> dict:
    if not header:
        return {}
    try:
        return json.loads(base64.b64decode(header))
    except (ValueError, json.JSONDecodeError):
        return {"raw": header[:200]}


async def buy(url: str, dry_run: bool) -> int:
    signer = _signer(dry_run)
    print(f"url:    {url}")
    print(f"payer:  {signer.address}" + ("  (throwaway, unfunded)" if dry_run else ""))

    client = register_exact_avm_client(x402Client(), signer)
    headers = {"accept": "application/json",
               # Identify yourself. Stock library defaults are commonly blocked
               # at the CDN edge, and you get a 403 instead of the 402 quote.
               "user-agent": "x402-avm-example/1.0"}
    if dry_run:
        headers["X-Selfcheck"] = "dry-run"

    async with x402HttpxClient(client, timeout=60.0) as http:
        r = await http.get(url, headers=headers)

    receipt = _receipt(r.headers.get("payment-response"))
    print(f"status: {r.status_code}")
    print(f"receipt: {json.dumps(receipt, indent=1)}")
    print(f"body:   {r.text[:300]}")

    if dry_run:
        # "the facilitator was called" is the whole point here, and the
        # simulation error is what proves it.
        ok = "simulation failed" in r.text or "missing from" in r.text
        print("RESULT:", "PATH ALIVE (refused for lack of funds, as expected)"
              if ok else "INCONCLUSIVE — read the seller's log")
        return 0 if ok else 1

    # Settlement, not optimism: HTTP 200 alone is not a sale. Only a receipt
    # carrying a transaction id proves the money moved.
    ok = r.status_code == 200 and bool(receipt.get("success")) and bool(receipt.get("transaction"))
    print("RESULT:", "SETTLED" if ok else "FAILED")
    if ok:
        print(f"verify: https://mainnet-idx.algonode.cloud/v2/transactions/{receipt['transaction']}")
    return 0 if ok else 1


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--dry-run"]
    if len(args) != 1:
        sys.exit(__doc__)
    sys.exit(asyncio.run(buy(args[0], "--dry-run" in sys.argv[1:])))
