"""Rede deduzida do FORMATO do endereco do pagador.

Importadores: traffic_log.py.

Ultimo recurso, nao primeira escolha: quando a linha do log traz `network=`,
e ela que manda -- o cliente declarou a rede e nao cabe adivinhar por cima.
Isto aqui so entra quando o campo falta, que e o caso de todo api.log
anterior a 06/09/2026 (commit 7cb74a4) e de qualquer emissor que volte a
esquece-lo. Sem isto o coletor carimba REDE_PADRAO em tudo, e foi assim que
uma recusa de Solana virou uma recusa de Base em 05/09/2026.

Deduzir nao e o mesmo que saber. So devolve rede quando o formato do endereco
e inconfundivel; no resto devolve None e quem chama decide o padrao.
"""

from __future__ import annotations

import re

BASE = "eip155:8453"
SOLANA = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
ALGORAND = "algorand:wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8="

# 0x + 40 hex. O tamanho importa: "0xabc" e um pedaco de texto, nao um
# endereco, e aceita-lo faria qualquer sujeira virar Base.
EVM_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
# Algorand: base32 (A-Z2-7) de 58 caracteres, checksum incluido.
ALGO_RE = re.compile(r"^[A-Z2-7]{58}$")
# Solana: base58 de 32 a 44. O alfabeto exclui 0, O, I e l de proposito.
SOL_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def deduzir(pagador) -> str | None:
    """CAIP-2 da rede a que o endereco pertence, ou None se nao der pra
    afirmar. `?` e vazio chegam aqui o tempo todo (self_send, erro sem
    pagador) e nao sao endereco de rede nenhuma."""
    if not pagador:
        return None
    valor = str(pagador).strip()
    if not valor or valor == "?":
        return None
    if EVM_RE.match(valor):
        return BASE
    # Algorand antes de Solana: 58 caracteres maiusculos passam pelos dois
    # alfabetos, e a faixa de Solana termina em 44 -- a ordem so importa se
    # alguem alargar SOL_RE depois.
    if ALGO_RE.match(valor):
        return ALGORAND
    if SOL_RE.match(valor):
        return SOLANA
    return None
