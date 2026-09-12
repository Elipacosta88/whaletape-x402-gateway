"""Robinhood Chain (eip155:4663) no x402: USDG pela Naven.

Por que um modulo
=================
As outras redes EVM (Base, Arbitrum) pagam em USDC da Circle, que a lib
`x402` conhece de fabrica. Robinhood Chain nao tem USDC nativo: a stablecoin
da rede e o USDG da Paxos, e a lib nao sabe o endereco nem o dominio EIP-712
dele. Este modulo ensina os dois, e aponta o facilitator que liquida la.

Fatos verificados no RPC publico da rede em 11/09/2026
=======================================================
- chainId 4663 (Arbitrum Orbit, gas em ETH; mainnet desde 01/07/2026).
- USDG: 0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168, 6 casas, proxy minimo
  cuja implementacao expoe TRANSFER_WITH_AUTHORIZATION_TYPEHASH (EIP-3009):
  o esquema `exact` padrao funciona nele.
- Dominio EIP-712 deduzido do DOMAIN_SEPARATOR real do contrato
  (0x7a3d7400...2036): name "Global Dollar", version "1". `version()` e
  `eip712Domain()` revertem, entao nao da para ler direto.
- Facilitator: a CDP nao lista a rede. A Naven (facilitator.naven.network)
  lista eip155:4663 e 46630 em v2 `exact`, sem chave. O servidor a coloca
  DEPOIS da CDP na lista: a lib da precedencia ao primeiro facilitator por
  rede, entao a Naven so recebe o que a CDP nao cobre.

Sem PAY_TO_ROBINHOOD no .env nada disto entra no 402.
"""

from __future__ import annotations

import os
from decimal import Decimal

import sys

from x402.http import FacilitatorConfig, HTTPFacilitatorClient, PaymentOption
from x402.schemas import AssetAmount, SupportedResponse

NETWORK = os.environ.get("ROBINHOOD_NETWORK", "eip155:4663")
NETWORK_TESTNET = "eip155:46630"
USDG = "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"
USDG_DECIMALS = 6
USDG_EIP712 = {"name": "Global Dollar", "version": "1"}
NAVEN_URL = os.environ.get("NAVEN_FACILITATOR_URL", "https://facilitator.naven.network").strip()
EXPLORER = "https://robinhoodchain.blockscout.com"


def parser_usdg(amount, network: str) -> AssetAmount | None:
    """MoneyParser da lib: "$0.001" em eip155:4663 vira 1000 unidades de USDG.

    Devolve None para qualquer outra rede, e a lib segue para o parser
    padrao (USDC). O `extra` leva o dominio EIP-712: sem ele o cliente assina
    com nome/versao errados e o facilitator recusa a autorizacao."""
    if str(network) != NETWORK:
        return None
    unidades = int((Decimal(str(amount)) * (10 ** USDG_DECIMALS)).to_integral_value())
    return AssetAmount(amount=str(unidades), asset=USDG, extra=dict(USDG_EIP712))


class FacilitatorTolerante:
    """Um facilitator secundario que, se estiver fora do ar no arranque, nao
    derruba os outros.

    A lib chama `get_supported()` de TODOS os clientes no primeiro pedido
    pago; se um levantar excecao, o middleware devolve 500 para toda rota
    paga, de toda rede, e tenta de novo a cada pedido. A Naven e um terceiro
    pequeno: se ela sumir, Robinhood Chain fica sem liquidacao (o comprador
    dessa rede leva erro), mas Base, Solana, Arbitrum e Algorand seguem
    vendendo. `verify`/`settle` passam direto: o erro deles e do pagamento em
    curso, e o SDK ja o trata."""

    def __init__(self, interno, log=None):
        self._interno = interno
        self._log = log or (lambda msg: print(msg, file=sys.stderr, flush=True))

    def get_supported(self):
        try:
            return self._interno.get_supported()
        except Exception as erro:  # noqa: BLE001 -- e exatamente o ponto
            self._log(f"[facilitator] Naven sem /supported no arranque ({erro!r}); "
                      f"Robinhood Chain fica sem facilitator ate o proximo restart")
            return SupportedResponse(kinds=[])

    async def verify(self, payload, requirements):
        return await self._interno.verify(sem_tags(payload), requirements)

    async def settle(self, payload, requirements):
        return await self._interno.settle(sem_tags(payload), requirements)


def sem_tags(payload):
    """Copia do envelope sem `resource.tags`.

    Medido em 11/09/2026 por bissecao contra o /verify da Naven: com o
    `resource` completo que o nosso 402 carrega (url, description, mimeType,
    serviceName, tags) ela responde 400 "Request must include matching
    x402Version, paymentPayload, and paymentRequirements"; tirando so `tags`
    ela passa a validar a assinatura. O `resource` nao entra na autorizacao
    EIP-3009 assinada pelo comprador, entao a copia continua valida. So a
    Naven ve esta versao; a CDP recebe o envelope intacto."""
    recurso = getattr(payload, "resource", None)
    if recurso is None or getattr(recurso, "tags", None) is None:
        return payload
    return payload.model_copy(update={"resource": recurso.model_copy(update={"tags": None})})


def cliente_naven(url: str | None = None) -> FacilitatorTolerante:
    """Facilitator da Naven, sem autenticacao, tolerante a queda no arranque."""
    return FacilitatorTolerante(HTTPFacilitatorClient(FacilitatorConfig(url=url or NAVEN_URL)))


def opcao(price: str, pay_to: str) -> PaymentOption:
    """A PaymentOption da rede: mesmo esquema `exact`, mesma carteira EVM."""
    return PaymentOption(scheme="exact", pay_to=pay_to, price=price, network=NETWORK)
