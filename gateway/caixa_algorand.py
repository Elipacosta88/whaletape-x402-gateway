"""Caixa Algorand na URL principal: o 402 de `/coverage` oferece Base E
Algorand, e quem paga pela Algorand e atendido pelo gateway separado
(ALGORAND_GATEWAY_URL).

Como funciona
=============
1. Requisicao SEM pagamento: segue a pilha normal. Se a resposta for o 402
   do SDK (cabecalho `payment-required`), acrescentamos ao `accepts` a opcao
   Algorand -- copiada do 402 que o gateway emite para o mesmo caminho, para
   bater byte a byte (scheme, rede, valor, ASA, payTo, feePayer). O SDK do
   gateway casa o pagamento exatamente por esses campos.
2. Requisicao COM pagamento cuja `accepted.network` comeca com `algorand:`:
   nao passa pelo caixa da API (que nao sabe Algorand) -- vai inteira para o
   gateway em loopback, que verifica, liquida no GoPlausible, busca o dado
   aqui pelo desvio interno e responde. Devolvemos status, corpo,
   content-type e `payment-response` dele.

Gateway fora do ar: o 402 sai so com Base (como antes) e um pagamento
Algorand recebe 502 sem cobrar nada. Nada aqui toca o caminho de Base.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
import urllib.error
import urllib.request

# Where the separate Algorand (AVM) gateway listens. Set ALGORAND_GATEWAY_URL
# to the address of your own deployment; the default below is a placeholder
# and points at nothing in particular.
URL_GATEWAY = os.getenv("ALGORAND_GATEWAY_URL") or "http://127.0.0.1:8080"
PREFIXO = "/algo"
CABECALHO_PAGAMENTO = "payment-signature"
CABECALHO_REQUISITOS = "payment-required"
CABECALHO_RESPOSTA = "payment-response"
TEMPO_402_S = 3.0
TEMPO_PAGAMENTO_S = 60.0
CACHE_S = 300.0

_cache_accepts: dict[str, tuple[float, list]] = {}


# ----------------------------------------------------------------- puros ----
def _json_b64(valor: str | None) -> dict | None:
    if not valor:
        return None
    try:
        dado = json.loads(base64.b64decode(valor))
    except (ValueError, json.JSONDecodeError):
        return None
    return dado if isinstance(dado, dict) else None


def rede_do_pagamento(cabecalho: str | None) -> str | None:
    """Rede declarada no payload v2 (`accepted.network`), ou None."""
    dado = _json_b64(cabecalho)
    if not dado:
        return None
    aceito = dado.get("accepted")
    rede = aceito.get("network") if isinstance(aceito, dict) else None
    return rede if isinstance(rede, str) else None


def e_pagamento_algorand(cabecalho: str | None) -> bool:
    return (rede_do_pagamento(cabecalho) or "").startswith("algorand:")


def accepts_do_402(cabecalho_gateway: str | None) -> list:
    """Do 402 do gateway, so as opcoes Algorand (lista nova)."""
    dado = _json_b64(cabecalho_gateway)
    if not dado:
        return []
    return [a for a in dado.get("accepts") or []
            if isinstance(a, dict) and str(a.get("network", "")).startswith("algorand:")]


def com_accepts_extra(requisitos: dict, extras: list) -> dict:
    """Copia do payload `payment-required` com as opcoes extras no fim."""
    if not extras:
        return requisitos
    novos = list(requisitos.get("accepts") or [])
    for a in extras:
        if a not in novos:
            novos.append(a)
    return {**requisitos, "accepts": novos}


def codifica(requisitos: dict) -> str:
    return base64.b64encode(json.dumps(requisitos, separators=(",", ":")).encode()).decode()


# No corpo v1 a outra rede vai aqui, nunca em `accepts`: o cliente legado
# (x402-fetch/x402-axios, pacote `x402` antigo) valida cada entrada de
# `accepts` com um enum fechado de redes e morre na Algorand antes de pagar.
# Medido em 2026-09-05: 14 tentativas v1 entre 30/08 e 01/09, zero desde que
# d7d45cb (02/09) passou a misturar. Mesmo campo que o manifesto ja usa.
CAMPO_ALTERNATIVAS_V1 = "alternatives"


def com_alternativas_v1(dado: dict, extras: list) -> dict:
    """Copia do corpo v1 com as opcoes extras em `alternatives`; `accepts` intacto."""
    atuais = list(dado.get(CAMPO_ALTERNATIVAS_V1) or [])
    for a in extras:
        if a not in atuais:
            atuais.append(a)
    return {**dado, CAMPO_ALTERNATIVAS_V1: atuais}


def corpo_com_accepts_extra(corpo: bytes, extras: list) -> bytes:
    """Se o corpo for o JSON do 402 (tem `accepts`), acrescenta; senao devolve igual.

    Uma rede nao pode mudar o que a outra anuncia: em corpo v1 a opcao extra vai
    em `alternatives`; em v2 (ou sem versao declarada) entra em `accepts`."""
    if not extras or not corpo:
        return corpo
    try:
        dado = json.loads(corpo)
    except (ValueError, json.JSONDecodeError):
        return corpo
    if not isinstance(dado, dict) or not isinstance(dado.get("accepts"), list):
        return corpo
    if dado.get("x402Version") == 1:
        return json.dumps(com_alternativas_v1(dado, extras)).encode()
    return json.dumps(com_accepts_extra(dado, extras)).encode()


def cabecalhos_ajustados(cabecalhos: list, requisitos_b64: str, tamanho: int) -> list:
    """Lista nova de cabecalhos ASGI com payment-required e content-length trocados."""
    saida = [(k, v) for k, v in cabecalhos
             if k.lower() not in (CABECALHO_REQUISITOS.encode(), b"content-length")]
    return saida + [(CABECALHO_REQUISITOS.encode(), requisitos_b64.encode()),
                    (b"content-length", str(tamanho).encode())]


# ------------------------------------------------------------ com rede ----
def _buscar_402(metodo: str, caminho: str) -> str | None:
    req = urllib.request.Request(f"{URL_GATEWAY}{PREFIXO}{caminho}", method=metodo,
                                 headers={"accept": "application/json",
                                          "user-agent": "whaletape-caixa-algorand/1"})
    try:
        with urllib.request.urlopen(req, timeout=TEMPO_402_S) as resp:
            return resp.headers.get(CABECALHO_REQUISITOS)
    except urllib.error.HTTPError as e:
        return e.headers.get(CABECALHO_REQUISITOS) if e.code == 402 else None


def accepts_algorand(metodo: str, caminho: str, agora: float | None = None,
                     buscar=_buscar_402) -> list:
    """Opcoes Algorand para (metodo, caminho), com cache curto por caminho."""
    agora = time.time() if agora is None else agora
    chave = f"{metodo} {caminho}"
    guardado = _cache_accepts.get(chave)
    if guardado and agora - guardado[0] < CACHE_S:
        return list(guardado[1])
    try:
        extras = accepts_do_402(buscar(metodo, caminho))
    except Exception as exc:  # gateway fora nao pode derrubar o 402 de Base
        print(f"[caixa algorand] sem 402 do gateway para {chave}: {exc}")
        extras = []
    _cache_accepts[chave] = (agora, extras)
    return list(extras)


def _repassar(metodo: str, caminho: str, query: bytes, cabecalhos: list, corpo: bytes):
    """Devolve (status, cabecalhos_de_resposta, corpo) do gateway."""
    url = f"{URL_GATEWAY}{PREFIXO}{caminho}" + (f"?{query.decode()}" if query else "")
    hdrs = {k.decode("latin-1"): v.decode("latin-1") for k, v in cabecalhos
            if k.lower() not in (b"host", b"content-length", b"connection")}
    req = urllib.request.Request(url, data=corpo or None, method=metodo, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=TEMPO_PAGAMENTO_S) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


class CaixaAlgorandASGI:
    def __init__(self, app, repassar=_repassar, accepts=accepts_algorand):
        self.app = app
        self._repassar = repassar
        self._accepts = accepts

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        cabecalhos = scope.get("headers") or []
        pagamento = next((v.decode("latin-1") for k, v in cabecalhos
                          if k.lower() == CABECALHO_PAGAMENTO.encode()), None)
        if e_pagamento_algorand(pagamento):
            return await self._atender_pelo_gateway(scope, receive, send, cabecalhos)
        return await self._com_402_ampliado(scope, receive, send)

    async def _atender_pelo_gateway(self, scope, receive, send, cabecalhos):
        corpo = b""
        while True:
            msg = await receive()
            corpo += msg.get("body", b"")
            if not msg.get("more_body"):
                break
        # Em thread, nunca no event loop: o gateway chama esta API de volta
        # para buscar o dado, e um loop bloqueado aqui vira deadlock ate o
        # timeout (medido em 02/09/2026: 502 nos dois lados).
        try:
            status, hdrs, dados = await asyncio.to_thread(
                self._repassar, scope["method"], scope["path"],
                scope.get("query_string", b""), cabecalhos, corpo)
        except Exception as exc:  # noqa: BLE001
            print(f"[caixa algorand] gateway indisponivel: {exc}", flush=True)
            status, hdrs, dados = 502, {"content-type": "application/json"}, \
                b'{"error":"algorand gateway unavailable","note":"no payment was taken"}'
        baixos = {k.lower(): v for k, v in hdrs.items()}
        saida = [(b"content-length", str(len(dados)).encode())]
        for nome in ("content-type", CABECALHO_RESPOSTA, CABECALHO_REQUISITOS):
            if nome in baixos:
                saida.append((nome.encode(), baixos[nome].encode("latin-1")))
        await send({"type": "http.response.start", "status": status, "headers": saida})
        await send({"type": "http.response.body", "body": dados})

    async def _com_402_ampliado(self, scope, receive, send):
        inicio: dict | None = None
        partes: list[bytes] = []

        async def enviar(msg):
            nonlocal inicio
            if msg["type"] == "http.response.start":
                hdrs = {k.lower(): v for k, v in msg.get("headers", [])}
                if msg["status"] == 402 and CABECALHO_REQUISITOS.encode() in hdrs:
                    inicio = msg  # segura ate ter o corpo inteiro
                    return
                return await send(msg)
            if inicio is None:
                return await send(msg)
            partes.append(msg.get("body", b""))
            if msg.get("more_body"):
                return
            await self._enviar_402(inicio, b"".join(partes), scope, send)

        await self.app(scope, receive, enviar)

    async def _enviar_402(self, inicio, corpo, scope, send):
        hdrs = {k.lower(): v for k, v in inicio.get("headers", [])}
        extras = await asyncio.to_thread(self._accepts, scope["method"], scope["path"])
        requisitos = _json_b64(hdrs[CABECALHO_REQUISITOS.encode()].decode("latin-1"))
        if not extras or not requisitos:
            await send(inicio)
            return await send({"type": "http.response.body", "body": corpo})
        novo_corpo = corpo_com_accepts_extra(corpo, extras)
        novos = cabecalhos_ajustados(inicio.get("headers", []),
                                     codifica(com_accepts_extra(requisitos, extras)),
                                     len(novo_corpo))
        await send({**inicio, "headers": novos})
        await send({"type": "http.response.body", "body": novo_corpo})
