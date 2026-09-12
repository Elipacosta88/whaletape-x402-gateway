"""The 402 itself: which schemes the server can verify, and what each route
advertises in `accepts`.

This is the core of the WhaleTape gateway. Everything else in the repository
exists to serve this file.
"""

from __future__ import annotations

from typing import Any

from x402.extensions.bazaar import (OutputConfig,
                                    bazaar_resource_server_extension,
                                    declare_discovery_extension)
from x402.http import PaymentOption
from x402.http.types import RouteConfig
from x402.mechanisms.evm.exact import ExactEvmServerScheme
from x402.server import x402ResourceServer

import config
import facilitators
import robinhood_chain
import rota_exemplo

SERVICE_NAME = "WhaleTape x402 Gateway"


def montar_servidor() -> x402ResourceServer:
    """Registers one scheme per ENABLED network.

    A network with no receiving address is never registered and therefore
    never advertised: the 402 must not offer what the server cannot verify."""
    server = x402ResourceServer(facilitators.construir())

    # Base (and Base Sepolia): plain EIP-3009 over Circle USDC.
    server.register(config.NETWORK, ExactEvmServerScheme())

    if config.PAY_TO_SOLANA:
        # The feePayer comes from the facilitator's /supported.
        from x402.mechanisms.svm.exact.register import register_exact_svm_server
        register_exact_svm_server(server, config.SOLANA_NETWORK)

    if config.PAY_TO_ARBITRUM:
        # Same EVM scheme as Base; the library resolves the USDC address of
        # eip155:42161 on its own.
        server.register(config.ARBITRUM_NETWORK, ExactEvmServerScheme())

    if config.PAY_TO_ROBINHOOD:
        # The library does not know USDG: this parser converts "$0.001" into
        # units AND puts the EIP-712 domain in `extra`. Only THIS instance of
        # the scheme has it -- Base must not inherit the USDG domain.
        esquema = ExactEvmServerScheme()
        esquema.register_money_parser(robinhood_chain.parser_usdg)
        server.register(config.ROBINHOOD_NETWORK, esquema)

    # Without this extension the CDP facilitator does not index the endpoints
    # in the Bazaar directory (it only catalogues 402s carrying
    # extensions.bazaar).
    server.register_extension(bazaar_resource_server_extension)
    return server


def opcoes_de_pagamento(price: str) -> list[PaymentOption]:
    """The payment options of a route, in the order the client will read them.

    The ORDER IS NOT A CONTRACT: anything that needs a specific network must
    match on the `network` field (see `v1_payments.opcao_da_rede`). Until
    2026-09-09 the v1 body read `accepts[0]` assuming it was Base, and
    reordering would have made the v1 402 advertise the Solana address on the
    Base network.
    """
    base = PaymentOption(scheme="exact", pay_to=config.PAY_TO_ADDRESS,
                         price=price, network=config.NETWORK)
    if not config.PAY_TO_SOLANA:
        opcoes = [base]
    else:
        solana = PaymentOption(scheme="exact", pay_to=config.PAY_TO_SOLANA,
                               price=price, network=config.SOLANA_NETWORK)
        opcoes = [solana, base] if config.REDE_PRIMEIRA == "solana" else [base, solana]
    if config.PAY_TO_ARBITRUM:
        # Always last: whoever already pays in Solana or Base sees nothing
        # move position.
        opcoes.append(PaymentOption(scheme="exact", pay_to=config.PAY_TO_ARBITRUM,
                                    price=price, network=config.ARBITRUM_NETWORK))
    if config.PAY_TO_ROBINHOOD:
        opcoes.append(robinhood_chain.opcao(price, config.PAY_TO_ROBINHOOD))
    return opcoes


def accepts_algorand_estatico(price_usd: str) -> list[dict[str, Any]]:
    """The Algorand `accepts` entry, built locally instead of fetched.

    NOT in the production file. In production the entry is copied byte for
    byte from the 402 that the separate Algorand gateway emits for the same
    path, because its SDK matches the payment on exactly those fields. This
    static version exists so that this repository can SHOW the five networks
    without shipping the AVM gateway -- it advertises correctly, but nothing
    here verifies or settles an Algorand payment. Only use it with
    `ALGORAND_GATEWAY_URL` empty, and only for demonstration.
    """
    if not config.PAY_TO_ALGORAND:
        return []
    unidades = str(int(round(float(str(price_usd).lstrip("$")) * 1_000_000)))
    extra = {"decimals": 6,
             "genesisHash": config.ALGORAND_NETWORK.split(":", 1)[1],
             "genesisId": "mainnet-v1.0"}
    if config.ALGORAND_FEE_PAYER:
        extra["feePayer"] = config.ALGORAND_FEE_PAYER
    return [{"scheme": "exact", "network": config.ALGORAND_NETWORK,
             "asset": config.ALGORAND_USDC_ASA, "amount": unidades,
             "payTo": config.PAY_TO_ALGORAND, "maxTimeoutSeconds": 300,
             "extra": extra}]


def rota_paga(price: str, description: str, tags: tuple[str, ...] = (),
              output_example: dict[str, Any] | None = None,
              input_example: dict[str, Any] | None = None,
              input_schema: dict[str, Any] | None = None,
              body_type: str | None = None) -> RouteConfig:
    """One priced route, with the discovery extension the directories index."""
    discovery = declare_discovery_extension(
        input=input_example,
        input_schema=input_schema,
        body_type=body_type,
        output=OutputConfig(example=output_example) if output_example else None,
    )
    return RouteConfig(
        accepts=opcoes_de_pagamento(price),
        mime_type="application/json",
        description=description,
        service_name=SERVICE_NAME,
        tags=list(tags),
        extensions=discovery,
    )


def url_anunciada(caminho: str) -> str:
    """The URL that goes to the directory. A parameterised route advertises a
    CALLABLE example, never the placeholder.

    Indexes publish whatever is here and the agent calls exactly that. With a
    placeholder the agent builds the payment and only discovers the error
    after signing -- that is how a sale evaporated on 2026-08-25."""
    return f"{config.SITE}{rota_exemplo.concreto(caminho) or caminho}"
