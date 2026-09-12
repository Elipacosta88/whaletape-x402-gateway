"""O middleware que faz um pagamento x402 v1 virar venda -- a metade que faltava.

Importadores: x402_market_data_api.py (registrado por FORA do middleware de
pagamento do SDK). Funcoes puras vivem em v1_payments.py; aqui fica so a parte
que mexe no ciclo ASGI, porque so se testa subindo o processo.

O problema
==========
Desde o commit 2e29173 (22/08/2026) a API anuncia requisitos v1 no corpo do
402, mas nunca soube receber o pagamento. 11 tentativas perdidas entre 24 e
31/08.

A causa, medida em 31/08 (`x402_http_server_base.py:890`): `_extract_payment`
e "V2 only" e le SOMENTE o cabecalho `PAYMENT-SIGNATURE`. O `X-PAYMENT`, que e
por onde todo cliente v1 manda o pagamento, e ignorado -- o servidor nem chega
a olhar. Sem payload extraido, a requisicao segue como "sem pagamento" e volta
402, em silencio, antes de qualquer casamento de rota ou chamada ao
facilitator.

(A hipotese anterior -- o casamento contra os `accepts` v2 falhando por causa
da rede legada "base" -- estava ERRADA, e foi descartada por medicao: o
casamento, quando alcancado, funciona. Ele nunca era alcancado.)

O SDK 2.21.0 (ultima no PyPI) tem ExactEvmSchemeV1Client e ...V1Facilitator,
mas nenhum V1Server.

A solucao: reembrulhar, nao desviar
===================================
Uma primeira versao deste middleware chamava `verify_payment` por conta
propria e depois repassava a requisicao para baixo. O verify passava -- e o
middleware do SDK, logo abaixo, continuava sem saber ler v1 e devolvia 402 do
mesmo jeito. Verificava e nao vendia. (O ensaio nao pegou porque media "o
facilitator foi chamado", nunca "a venda completou"; com carteira sem saldo os
dois eram indistinguiveis.)

Agora o pagamento v1 e traduzido para o envelope v2 e reapresentado no
cabecalho `PAYMENT-SIGNATURE`, que e o unico que o servidor le: a
autorizacao EIP-3009 assinada e a mesma (a assinatura nao cobre a rede nem o
`resource`), entao verify, liquidacao, cabecalho de resposta e contabilidade
passam a ser exatamente os do v2 -- que vende desde sempre. Nao existe segundo
caminho de liquidacao, que seria um segundo lugar para errar com dinheiro.

Os requisitos v2 usados no envelope vem do proprio servidor: rodamos a
requisicao uma vez sem pagamento so para ler o cabecalho `payment-required` do
402 que o SDK gera. Custa um 402 interno (sem rede, sem facilitator) e evita
manter aqui uma copia dos requisitos -- copia que ja divergiu uma vez.

Em qualquer duvida -- payload ilegivel, rota desconhecida, 402 sem requisitos
v2 -- a requisicao segue para o fluxo normal e o cliente recebe o 402 de
sempre. Falhar para o lado do 402 custa uma venda; falhar para o outro entrega
o produto de graca.
"""

from __future__ import annotations

from x402.http.utils import decode_payment_required_header

import v1_payments

# Motivo legivel -> token de uma palavra. A linha `[x402 recusa]` e lida pelo
# traffic_log com `key.split(" ")`: motivo com espaco desalinha as colunas e a
# recusa entra no banco com `reason` e `payer` trocados.
_TOKENS = {
    "payload ilegivel": "payload_ilegivel",
    "payload interno ausente": "payload_interno_ausente",
    "authorization ausente": "authorization_ausente",
    "scheme diferente do anunciado": "scheme_diferente",
    "rede diferente da anunciada": "rede_diferente",
    "pagamento enderecado a outra carteira": "outra_carteira",
    "valor ilegivel": "valor_ilegivel",
    "valor abaixo do preco da rota": "valor_abaixo_do_preco",
}


def token_da_recusa(motivo: str) -> str:
    """Token de uma palavra para o motivo. Motivo novo vira token cru em vez de
    sumir: recusa com nome feio e melhor que recusa invisivel."""
    conhecido = _TOKENS.get(motivo)
    if conhecido:
        return conhecido
    return "_".join(str(motivo).split())[:60] or "?"


def pagador_do_payload(payload) -> str | None:
    """Endereco de quem assinou, ou None quando o payload nao chega la.

    Vale a escavacao: sem o pagador o vigia nao consegue perguntar a Base se
    aquela carteira tinha saldo, e recusa de carteira vazia (que nao e defeito
    nosso) chega ao alarme igual a um caminho de pagamento quebrado."""
    if not isinstance(payload, dict):
        return None
    interno = payload.get("payload")
    if not isinstance(interno, dict):
        return None
    auth = interno.get("authorization")
    if not isinstance(auth, dict):
        return None
    quem = auth.get("from")
    return str(quem) if quem else None


class PagamentoV1ASGI:
    """Traduz pagamento x402 v1 para v2 antes que o SDK o descarte.

    Registrado por FORA do middleware de pagamento, entao ve a requisicao antes
    dele. So age quando ha X-PAYMENT declarando v1 e NAO ha PAYMENT-SIGNATURE:
    um pagamento v2 nunca passa por aqui, e por isso o caminho que hoje vende
    fica intocado.
    """

    def __init__(self, app, rotas, rede_caip2, url_anunciada,
                 conta_tentativa=None):
        self.app = app
        self.rotas = rotas
        self.rede = rede_caip2
        self.url_anunciada = url_anunciada
        # Quando esta requisicao NAO entrou em payment_attempt_log (auto-teste,
        # carteira propria), a recusa dela tambem nao pode entrar: o vigia
        # subtrai recusa de tentativa, e descontar o que nunca somou faz a
        # conta de "quem tentou pagar e nao conseguiu" encolher para menos que
        # a realidade -- alarme que emudece sozinho. Por padrao loga: um
        # registro a mais e ruido, um a menos e a cegueira de 09/09/2026.
        self.conta_tentativa = conta_tentativa or (lambda: True)

    def _recusa(self, path, rede, razao, pagador) -> None:
        """Uma linha `[x402 recusa]` para o traffic_log virar serie temporal.

        `version=1` sempre: e o que o cabecalho X-PAYMENT declara e o que a
        linha `[x402 payment]` correspondente gravou. Casar as duas importa
        mais que registrar a versao que o corpo dizia -- essa vai na linha
        `[v1]` ao lado, que e para humano."""
        try:
            if not self.conta_tentativa():
                return
        except Exception:
            pass  # porteiro quebrado nao pode calar a recusa
        rede_txt = f" network={rede}" if rede else ""
        print(f"[x402 recusa] version=1 path={path or '?'}{rede_txt} "
              f"reason={razao or '?'} payer={pagador or '?'}", flush=True)

    def _requisitos_v1(self, chave):
        # Mesma chamada que monta o corpo do 402 anunciado. Ver
        # v1_payments.requisitos_da_rota: o ponto e nao haver duas versoes.
        return v1_payments.requisitos_da_rota(chave, self.rotas[chave],
                                              self.rede, self.url_anunciada)

    @staticmethod
    def _com_headers(scope, headers):
        """Copia rasa do scope com outra lista de cabecalhos.

        Copia porque o scope e compartilhado com o que roda depois; mutar a
        lista original vazaria a troca do X-PAYMENT para fora da requisicao."""
        novo = dict(scope)
        novo["headers"] = headers
        return novo

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        headers = scope.get("headers", [])
        h = {k.decode("latin-1").lower(): v for k, v in headers}
        if "payment-signature" in h or "x-payment" not in h:
            return await self.app(scope, receive, send)  # v2 ou sem pagamento

        path = scope.get("path", "")
        payload = v1_payments.payload_do_cabecalho(h["x-payment"])
        if payload is None:
            self._recusa(path, None, "payload_ilegivel", None)
            return await self.app(scope, receive, send)

        versao = payload.get("x402Version")
        pagador = pagador_do_payload(payload)
        rede = payload.get("network")

        # Envelope v2 chegando no X-PAYMENT. Nao e hipotese: a sonda A/B de
        # 09/09/2026 mandou os MESMOS bytes nos dois cabecalhos -- no
        # PAYMENT-SIGNATURE chegou ao facilitator, no X-PAYMENT sumiu sem uma
        # linha de log. O SDK so le PAYMENT-SIGNATURE e este middleware so
        # aceitava x402Version == 1, entao o pagamento caia no vao entre os
        # dois. Reapresentar os mesmos bytes no cabecalho que o SDK le nao
        # inventa caminho de liquidacao nenhum: e o do v2, que vende sempre.
        if versao == 2:
            trocados = list(headers) + [(b"payment-signature", h["x-payment"])]
            print(f"[v1] envelope v2 no X-PAYMENT reapresentado como "
                  f"PAYMENT-SIGNATURE (path {path})", flush=True)
            return await self.app(self._com_headers(scope, trocados),
                                  receive, send)

        if versao != 1:
            self._recusa(path, rede, "versao_desconhecida", pagador)
            print(f"[v1] x402Version {versao!r} nao e 1 nem 2 (path {path})",
                  flush=True)
            return await self.app(scope, receive, send)

        chave = v1_payments.casa_rota(scope.get("method", ""), path, self.rotas)
        if chave is None:
            self._recusa(path, rede, "rota_nao_paga", pagador)
            return await self.app(scope, receive, send)

        # Conferencia nossa antes de gastar qualquer ida de rede: o pagamento
        # tem de ser para ESTA rota e por ESTE preco. Sem isto, um pagamento
        # assinado para /coverage ($0.001) reembrulharia como pagamento de
        # /premium ($0.25).
        motivo = v1_payments.recusa(payload, self._requisitos_v1(chave))
        if motivo:
            self._recusa(path, rede, token_da_recusa(motivo), pagador)
            print(f"[v1] recusado sem chamar o facilitator: {motivo}", flush=True)
            return await self.app(scope, receive, send)

        # O corpo so pode ser lido uma vez do `receive` original, e vamos rodar
        # a aplicacao duas vezes. Sem isto o POST /alerts perderia o corpo na
        # segunda passagem e seria recusado por webhook ausente.
        corpo = []
        while True:
            msg = await receive()
            corpo.append(msg)
            if msg["type"] != "http.request" or not msg.get("more_body"):
                break

        def repetidor():
            restante = list(corpo)

            async def _receive():
                if restante:
                    return restante.pop(0)
                return {"type": "http.disconnect"}
            return _receive

        aceito = await self._requisitos_v2_anunciados(scope, headers, repetidor())
        if aceito is None:
            print("[v1] nao consegui ler os requisitos v2 do 402; "
                  "seguindo pro 402 normal", flush=True)
            return await self.app(scope, repetidor(), send)

        # PAYMENT-SIGNATURE, e nao X-PAYMENT: `_extract_payment` do SDK
        # (x402_http_server_base.py:890) e "V2 only" e so le este cabecalho.
        # Reescrever o X-PAYMENT nao adiantaria nada -- ele nunca e lido, que e
        # a razao de o v1 nunca ter funcionado. O X-PAYMENT original segue
        # intacto: o SDK o ignora, e quem loga pagamento continua vendo o que o
        # cliente realmente mandou.
        novo_header = v1_payments.cabecalho_x_payment(
            v1_payments.traduz_para_v2(payload, aceito))
        trocados = list(headers) + [(b"payment-signature", novo_header)]

        print(f"[v1] traduzido para v2 e seguindo como pagamento normal "
              f"(rota {chave})", flush=True)
        return await self.app(self._com_headers(scope, trocados),
                              repetidor(), send)

    async def _requisitos_v2_anunciados(self, scope, headers, receive):
        """Os `accepts` v2 que o proprio servidor anuncia para esta rota.

        Roda a requisicao sem o X-PAYMENT so para colher o cabecalho
        `payment-required` do 402. Nao chama facilitator e a resposta e
        descartada -- nada disso chega ao cliente nem as metricas."""
        sem_pagamento = [(k, v) for k, v in headers
                         if k.decode("latin-1").lower() != "x-payment"]
        colhido = {}

        async def _send(msg):
            if msg["type"] == "http.response.start":
                colhido["headers"] = msg.get("headers", [])
                colhido["status"] = msg["status"]

        try:
            await self.app(self._com_headers(scope, sem_pagamento), receive, _send)
            if colhido.get("status") != 402:
                return None
            bruto = next((v for k, v in colhido.get("headers", [])
                          if k.decode("latin-1").lower() == "payment-required"), None)
            if not bruto:
                return None
            req = decode_payment_required_header(bruto.decode("latin-1"))
            aceita = req.accepts
            if not aceita:
                return None
            return aceita[0].model_dump(by_alias=True, exclude_none=True)
        except Exception as e:  # noqa: BLE001
            print(f"[v1] falha lendo requisitos v2 ({type(e).__name__}: {str(e)[:120]})",
                  flush=True)
            return None
