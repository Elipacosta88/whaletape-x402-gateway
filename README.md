# WhaleTape x402 Gateway

The payment layer of [whaletape.xyz](https://whaletape.xyz), extracted as a
standalone FastAPI server: **one HTTP endpoint that charges per request in
stablecoin, across five chains, and settles on-chain before it answers.**

This is a cut of production code, not a demo written for a submission. The
comments explain why each decision is the way it is, usually with the date and
the incident that caused it. Published for the **Arbitrum Open House
(Singapore)**.

What is *not* here: the signal engine, the data collector, the database, the
operational dashboards, the logs and the wallets. This repository is the
gateway and nothing else.

## What x402 is

HTTP 402 Payment Required, made usable. The server answers an unpaid request
with a **quote**: which chains it accepts, which asset, how much, and to which
address. The client — in practice a software agent, not a human — signs a
payment authorisation, replays the request with the signature in a header, and
a **facilitator** verifies and settles it on-chain. The server only returns
the product after settlement.

No account, no API key, no KYC, no invoice. The customer is a wallet address.

## Networks

| Network | CAIP-2 | Asset | Facilitator |
|---|---|---|---|
| Base | `eip155:8453` | USDC (Circle, native) | Coinbase CDP, PayAI as fallback |
| Arbitrum One | `eip155:42161` | USDC (Circle, native) | Coinbase CDP, PayAI as fallback |
| Solana | `solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp` | USDC | Coinbase CDP |
| Robinhood Chain | `eip155:4663` | USDG (Paxos, "Global Dollar") | Naven |
| Algorand | `algorand:wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8=` | USDC (ASA 31566704) | GoPlausible |

All five are mainnet and all five have settled real payments in production.

Both **x402 v1 and v2** are served on the same routes. v2 clients send
`PAYMENT-SIGNATURE`; v1 clients send `X-PAYMENT` and read the quote from the
response *body*. The SDK only reads v2, so `gateway/v1_middleware.py`
translates — see "Things this code knows" below.

## Layout

```
main.py                        minimal server: one paid route, five networks
example_route.py               the product being sold (static payload here)
gateway/
  config.py                    every network is opt-in via .env
  payment_options.py           scheme registration and what `accepts` says
  facilitators.py              CDP client, fallback chain, verify/settle logging
  facilitator_reserva.py       failover to a second facilitator, narrowly
  robinhood_chain.py           USDG: money parser, EIP-712 domain, Naven client
  caixa_algorand.py            splices the Algorand option in, proxies payments
  v1_middleware.py             makes a legacy x402 v1 payment a real sale
  v1_payments.py               pure functions behind the above
  rota_exemplo.py              parameterised route -> callable example URL
  rede_do_pagador.py           infer the chain from the payer address format
```

## Running it

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env      # fill in the public addresses; no private keys
.venv/bin/python main.py  # http://127.0.0.1:8402
```

With no `NETWORK` set it runs on Base Sepolia against the free public
facilitator: you can exercise the whole flow on testnet before touching
mainnet. With no `PAY_TO_ADDRESS` it refuses to boot.

Look at the quote:

```bash
curl -s http://127.0.0.1:8402/                # free: routes and networks
curl -si http://127.0.0.1:8402/coverage       # 402 + `payment-required` header
```

The v2 quote lives in the base64 `payment-required` header; the v1 quote is
the JSON body of the same response.

## Buying from it

```js
import { wrapFetchWithPayment } from "x402-fetch";
import { privateKeyToAccount } from "viem/accounts";

const account = privateKeyToAccount(process.env.PRIVATE_KEY);
const fetchWithPay = wrapFetchWithPayment(fetch, account);

const res = await fetchWithPay("https://whaletape.xyz/coverage");
console.log(await res.json());
```

The wallet needs USDC *and* the chain's gas token. The single most common
failure in production is neither: an empty wallet that signs a valid
authorisation the facilitator then refuses.

## Things this code knows, that cost something to learn

- **The SDK only reads `PAYMENT-SIGNATURE`.** A v1 client sends `X-PAYMENT`,
  and the SDK discards it *before* any route matching or facilitator call —
  silently, with no log line. Eleven signed payments from one agent were lost
  this way. `v1_middleware.py` re-wraps the v1 payment into the v2 envelope
  instead of building a second settlement path: one code path for money, one
  place to get it wrong.
- **One network must never change what another advertises.** A legacy client
  validates every entry of `accepts` against a closed enum of networks and
  dies on an unknown one *before paying*. So Algorand goes into
  `alternatives` for a v1 body and into `accepts` for v2.
- **The order of `accepts` decides the chain of the sale**, because most
  clients pay the first offer they understand. It is therefore an experiment
  with a revert switch (`X402_REDE_PRIMEIRA`), not a constant. Nothing else
  may read `accepts[0]` and assume a chain — match on the `network` field.
- **Never advertise a placeholder URL.** Directories publish the `resource`
  field verbatim and agents call exactly that. `/history/[symbol]` makes an
  agent build a payment for a path that 404s; it signs first and finds out
  after. `rota_exemplo.py` maps every parameterised route to a callable
  example.
- **A route that is not ready must answer >= 400.** The middleware does not
  settle on responses >= 400, so a `503 warming up` is free for the buyer,
  while a `200 []` takes their money and loses them forever.
- **Robinhood Chain needed two things the library did not know**: the USDG
  address with its EIP-712 domain (`name: "Global Dollar"`, `version: "1"`,
  recovered from the contract's `DOMAIN_SEPARATOR`, because `version()` and
  `eip712Domain()` both revert), and a facilitator that lists the chain. Naven
  also rejects the envelope unless `resource.tags` is stripped — found by
  bisection against its `/verify`.
- **Failover between facilitators is narrower than it looks.** Only on
  transport or server failure, never on a 4xx refusal, and only where the
  reserve can settle what the primary quoted. On Solana the signed transaction
  embeds the primary's `feePayer`, so failover is impossible by construction.

## Algorand

Algorand cannot share this process: its scheme ships in the `x402-avm`
distribution, which **shadows** the `x402` package — installing both in one
venv breaks the other four chains. In production it runs as a separate service
and `caixa_algorand.py` copies its 402 option, byte for byte, into this one.
With `ALGORAND_GATEWAY_URL` empty, this repository advertises a locally built
Algorand entry for demonstration only: it quotes correctly, but this process
does not verify or settle Algorand.
Set `ALGORAND_GATEWAY_URL` to the base URL of your own AVM gateway (for
example `http://127.0.0.1:8080`) to advertise and settle for real.

## Licence

Business Source License 1.1. Non-production use is free; production use needs
a licence from the Licensor until the Change Date (2030-09-14), when it
becomes Apache 2.0. See `LICENSE`.
