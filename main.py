"""Minimal x402 server: one paid route, five networks.

Middleware order is the whole game, and it reads INSIDE-OUT: the last
`add_middleware` call runs FIRST on the way in. From outermost to innermost:

    CaixaAlgorandASGI    -- adds the Algorand option to the 402 and routes an
                            Algorand payment to the separate AVM gateway
    PagamentoV1ASGI      -- translates a legacy x402 v1 payment (X-PAYMENT)
                            into the v2 envelope the SDK can read
    PaymentMiddlewareASGI-- the SDK's paywall: quotes, verifies, settles
    FastAPI routes       -- the product

The v1 layer must sit OUTSIDE the SDK paywall, because the SDK reads only
`PAYMENT-SIGNATURE`; a v1 client sends `X-PAYMENT` and its payment was
discarded in silence before any facilitator was called. That was 11 lost
sales between 2026-08-24 and 2026-08-31.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent / "gateway"))

import uvicorn
from fastapi import FastAPI
from x402.http.middleware.fastapi import PaymentMiddlewareASGI
from x402.http.types import HTTPResponseBody, PaywallConfig, RouteConfig

import caixa_algorand
import config
import example_route
import payment_options
import v1_payments
from v1_middleware import PagamentoV1ASGI

EXAMPLE_PATH = "/coverage"
ROUTE_KEY = f"GET {EXAMPLE_PATH}"

app = FastAPI(title=payment_options.SERVICE_NAME,
              docs_url=None, redoc_url=None, openapi_url=None)


@app.get(EXAMPLE_PATH)
def coverage():
    return example_route.payload()


@app.get("/")
def index():
    """Free: what this server sells, and on which networks."""
    return {
        "service": payment_options.SERVICE_NAME,
        "paid_routes": [{"method": "GET", "path": EXAMPLE_PATH,
                         "price": example_route.PRICE,
                         "description": example_route.DESCRIPTION}],
        "networks": [a["network"] for a in _accepts_anunciados()],
        "x402_versions": [1, 2],
        "how_to_pay": f"GET {EXAMPLE_PATH} without payment returns 402 with the "
                      f"quote in the `payment-required` header (v2) and in the "
                      f"body (v1).",
    }


def _accepts_anunciados() -> list[dict[str, Any]]:
    """Every network this server advertises, Algorand included."""
    saida = [{"network": o.network, "payTo": o.pay_to}
             for o in payment_options.opcoes_de_pagamento(example_route.PRICE)]
    saida += [{"network": a["network"], "payTo": a["payTo"]}
              for a in _accepts_algorand("GET", EXAMPLE_PATH)]
    return saida


# --------------------------------------------------------------- routes ----
server = payment_options.montar_servidor()

routes = {ROUTE_KEY: payment_options.rota_paga(
    price=example_route.PRICE,
    description=example_route.DESCRIPTION,
    tags=example_route.TAGS,
    output_example=example_route.OUTPUT_EXAMPLE)}


def _v1_unpaid_body(chave: str,
                    cfg: RouteConfig) -> Callable[[Any], HTTPResponseBody]:
    """The v1 body of the 402, built by the SAME function the v1 middleware
    uses to CHECK the payment.

    These were two similar blocks and they had already diverged. The client
    signs exactly what is advertised, so advertising and checking must come
    from one place."""
    corpo = v1_payments.corpo_402_v1(
        v1_payments.requisitos_da_rota(chave, cfg, config.NETWORK,
                                       payment_options.url_anunciada))
    return lambda _ctx: HTTPResponseBody(content_type="application/json", body=corpo)


for _chave, _cfg in routes.items():
    _cfg.unpaid_response_body = _v1_unpaid_body(_chave, _cfg)
    # Without `resource` the SDK advertises the request path, which on a
    # parameterised route is the placeholder itself.
    _cfg.resource = payment_options.url_anunciada(_chave.split(" ", 1)[1])


# ------------------------------------------------------------ Algorand ----
def _accepts_algorand(metodo: str, caminho: str) -> list[dict[str, Any]]:
    """Algorand options for (method, path).

    With ALGORAND_GATEWAY_URL set, they are copied byte for byte from the 402
    of the separate AVM gateway -- which is what production does, because that
    gateway's SDK matches the payment on exactly those fields. Without it, a
    locally built entry is advertised for DEMONSTRATION ONLY: this process
    cannot verify or settle an Algorand payment."""
    if config.ALGORAND_GATEWAY_URL:
        return caixa_algorand.accepts_algorand(metodo, caminho)
    return payment_options.accepts_algorand_estatico(example_route.PRICE)


if config.ALGORAND_GATEWAY_URL:
    caixa_algorand.URL_GATEWAY = config.ALGORAND_GATEWAY_URL

# ------------------------------------------------------- middleware stack ----
app.add_middleware(
    PaymentMiddlewareASGI,
    routes=routes,
    server=server,
    # A browser that lands on a paid route gets the SDK's "pay with wallet"
    # page instead of the raw 402 JSON.
    paywall_config=PaywallConfig(app_name=payment_options.SERVICE_NAME),
)

# Outside the SDK paywall = sees the v1 payment before the SDK's route
# matching discards it in silence.
app.add_middleware(PagamentoV1ASGI, rotas=routes,
                   rede_caip2=config.NETWORK,
                   url_anunciada=payment_options.url_anunciada)

# Outermost: the Algorand cashier. It splices the Algorand option into the
# 402 (into `alternatives` for a v1 body, into `accepts` for v2 -- a legacy
# client validates every `accepts` entry against a closed enum of networks and
# dies on Algorand before paying) and forwards an Algorand payment to the AVM
# gateway.
if config.PAY_TO_ALGORAND:
    app.add_middleware(caixa_algorand.CaixaAlgorandASGI,
                       accepts=lambda m, c, **_kw: _accepts_algorand(m, c))


if __name__ == "__main__":
    uvicorn.run(app, host=config.BIND_HOST, port=config.PORT)
