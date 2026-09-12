"""Facilitator primario com reserva: tenta o proximo so quando o primeiro CAIU.

Por que
=======
O SDK roteia por (rede, esquema) para UM cliente e nao tenta outro em falha.
Se a Coinbase oscila, o comprador leva erro e vai embora -- e ninguem nos
avisa, porque 402/5xx nao e venda perdida em nenhuma metrica. O Syra roda
Dexter -> GoPlausible -> PayAI em cadeia e tem 1,5% de falha de settle.

Tres regras, e as tres sao estreitas de proposito
================================================
1. So cai para a reserva em falha de TRANSPORTE ou de SERVIDOR: timeout,
   conexao recusada, 5xx, 429. Recusa 4xx ("insufficient funds",
   "invalid_payload") e resposta legitima sobre o pagamento -- repetir noutro
   facilitator so mascararia o motivo.
2. So para redes em que a reserva consegue liquidar o que o primario COTOU.
   Em Base a autorizacao EIP-3009 vale para qualquer facilitator. Em Solana a
   transacao assinada embute o `feePayer` anunciado no 402 (o da Coinbase), e
   outro facilitator nao tem essa chave: failover ali e impossivel por
   construcao. Algorand e so GoPlausible.
3. `get_supported` vem sempre do primario: e dele o feePayer e as extensoes
   que o 402 anuncia. A reserva nunca muda a cara da cotacao.

Liquidacao dupla nao acontece: se a Coinbase liquidou mas a resposta se
perdeu, a reserva tenta a MESMA autorizacao (mesmo nonce) e a rede recusa.
"""

from __future__ import annotations

import re
import sys

import httpx

# Base e Arbitrum One: nas duas a autorizacao EIP-3009 do USDC nativo vale
# para qualquer facilitator, e a PayAI lista as duas em v2 exact.
REDES_COM_RESERVA = ("eip155:8453", "eip155:42161")
_STATUS_NA_MENSAGEM = re.compile(r"failed \((\d{3})\)")


def e_falha_de_transporte(erro: Exception) -> bool:
    """A falha e do caminho (rede/servidor), nao do pagamento?"""
    if isinstance(erro, httpx.HTTPError):  # timeout, connect, protocolo
        return True
    m = _STATUS_NA_MENSAGEM.search(str(erro))
    if m:
        status = int(m.group(1))
        return status >= 500 or status == 429
    return False


class FacilitatorEncadeado:
    """Implementa o Protocol FacilitatorClient (verify, settle, get_supported)."""

    def __init__(self, primario, reservas, redes_com_reserva=REDES_COM_RESERVA,
                 log=None):
        self.primario = primario
        self.reservas = list(reservas or ())
        self.redes_com_reserva = tuple(redes_com_reserva)
        self._log = log or (lambda msg: print(msg, file=sys.stderr, flush=True))

    def get_supported(self):
        return self.primario.get_supported()

    async def verify(self, payload, requirements):
        return await self._tentar("verify", payload, requirements)

    async def settle(self, payload, requirements):
        return await self._tentar("settle", payload, requirements)

    async def _tentar(self, operacao: str, payload, requirements):
        try:
            return await getattr(self.primario, operacao)(payload, requirements)
        except Exception as erro:  # noqa: BLE001 -- decidir aqui e o ponto
            rede = str(getattr(requirements, "network", "") or "")
            if not (self.reservas and e_falha_de_transporte(erro)
                    and rede in self.redes_com_reserva):
                raise
            ultimo = erro
            for i, reserva in enumerate(self.reservas, 1):
                self._log(f"[facilitator] primario falhou no {operacao} ({erro!r}); "
                          f"tentando reserva {i} em {rede}")
                try:
                    resultado = await getattr(reserva, operacao)(payload, requirements)
                    self._log(f"[facilitator] reserva {i} respondeu ao {operacao}")
                    return resultado
                except Exception as erro_reserva:  # noqa: BLE001
                    ultimo = erro_reserva
            raise ultimo


def encadear(primario, reservas, **kw):
    """Sem reservas devolve o primario intacto: caminho de hoje, byte a byte."""
    return FacilitatorEncadeado(primario, reservas, **kw) if reservas else primario
