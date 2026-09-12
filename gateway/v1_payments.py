"""Aceitar pagamento x402 v1 -- a metade que faltava do commit 2e29173.

Por que este modulo existe
==========================
Desde 22/08/2026 a API anuncia os requisitos em formato v1 no corpo do 402
(`_v1_unpaid_body`), com a justificativa de que "o servidor ja aceita
PaymentPayloadV1". Nao aceitava. O middleware do SDK casa o pagamento contra
os `accepts` da rota, que so existem em v2 (`eip155:8453`); um pagamento v1
declara a rede legada ("base"), o casamento falha e a requisicao volta como
402 -- em silencio, sem uma linha de log. Entre 24 e 31/08 isso aconteceu 11
vezes com o mesmo agente, que assinou, apanhou e nunca comprou.

Registrar a rede legada no servidor NAO resolve: medido em ensaio, o
pagamento v1 continua morrendo antes, no casamento de rotas, e declarar um
`accepts` legado nem sobe ("Facilitator does not support scheme exact on
network base-sepolia" -- a validacao da rota so enxerga kinds v2).

O que funciona, e o que este modulo prepara: pular o casamento do middleware e
chamar `server.verify_payment(payload_v1, requisitos_v1)` direto. Medido: a
chamada chega ao facilitator da CDP, que responde com veredito real e
identifica o pagador.

Aqui moram so funcoes puras -- casar rota, montar requisitos, decodificar
payload e recusar o que nao bate. A parte que fala com o facilitator vive no
middleware, porque so se testa subindo o processo.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from decimal import Decimal

# Nome legado da rede, por rede CAIP-2. E o que um pagamento v1 declara.
REDE_LEGADA = {"eip155:8453": "base", "eip155:84532": "base-sepolia"}

USDC_POR_REDE = {"eip155:8453": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                 "eip155:84532": "0x036CbD53842c5426634e7929541eC2318f3dCF7e"}

# Um pagamento v1 cabe folgado nisso. Acima disso e lixo ou ataque, e nao vale
# gastar CPU decodificando base64 gigante antes de recusar.
_PAYLOAD_MAX_BYTES = 8192

_cache_regex: dict[str, re.Pattern] = {}


def _regex_da_rota(caminho: str) -> re.Pattern:
    """`/markets/class/[asset_class]` -> regex que casa `/markets/class/crypto`.

    So `[nome]` vira curinga, e o curinga nunca atravessa `/`: sem isso
    `/whale/[address]` casaria `/whale/0xabc/extra`, e o pagamento de uma rota
    valeria por outra."""
    if caminho not in _cache_regex:
        partes = [re.escape(p) if not (p.startswith("[") and p.endswith("]"))
                  else r"[^/]+"
                  for p in caminho.split("/")]
        _cache_regex[caminho] = re.compile("^" + "/".join(partes) + "$")
    return _cache_regex[caminho]


def casa_rota(metodo: str, caminho: str, chaves) -> str | None:
    """Chave de rota ("GET /markets/class/[asset_class]") do caminho pedido.

    Prefere a rota literal quando existe: `/coverage` e `/coverage/[symbol]`
    convivem, e a exata tem que ganhar da parametrizada."""
    alvo = f"{(metodo or '').upper()} {caminho}"
    if alvo in chaves:
        return alvo
    for chave in chaves:
        m, _, padrao = chave.partition(" ")
        if m == (metodo or "").upper() and _regex_da_rota(padrao).match(caminho):
            return chave
    return None


def requisitos_v1(preco_usd: str, pay_to: str, rede_caip2: str, recurso: str,
                  descricao: str, mime: str = "application/json",
                  timeout_s: int = 300, scheme: str = "exact") -> dict:
    """Os requisitos v1 de uma rota -- os MESMOS que o 402 ja anuncia.

    Tem de ser identico ao corpo anunciado: o cliente assina exatamente aquilo,
    e um centavo de diferenca faz o facilitator recusar uma assinatura boa.

    `Decimal` e nao `float` porque e caminho de dinheiro e porque a producao
    converte assim: float("0.001") * 1_000_000 da 1000.0000000000001, e duas
    rotas de arredondamento diferentes sao exatamente o tipo de divergencia
    que so aparece na primeira venda perdida."""
    centavos = str(int(Decimal(str(preco_usd).lstrip("$")) * 1_000_000))
    return {
        "scheme": scheme,
        "network": REDE_LEGADA.get(rede_caip2, rede_caip2),
        "maxAmountRequired": centavos,
        "resource": recurso,
        "description": descricao,
        "mimeType": mime,
        "payTo": pay_to,
        "maxTimeoutSeconds": timeout_s,
        "asset": USDC_POR_REDE.get(rede_caip2, ""),
        "extra": {"name": "USD Coin", "version": "2"},
    }


def opcao_da_rede(cfg, rede_caip2: str):
    """A opcao de pagamento DAQUELA rede -- nunca a primeira da lista.

    Ate 09/09/2026 isto era `cfg.accepts[0]`, apoiado no contrato implicito
    "Base primeiro, SEMPRE". O corpo v1 combina o `pay_to` desta opcao com a
    `rede_caip2` que vem por parametro: se a ordem mudar e o indice 0 virar
    Solana, o 402 v1 passa a mandar pagar o ENDERECO SOLANA NA REDE BASE. O
    cliente assina, o dinheiro sai, e vai para um endereco que ninguem
    controla naquela rede.

    Casar pela rede em vez do indice desacopla a ordem da correcao, que e o
    que permite testar Solana primeiro sem tocar no caminho v1."""
    for opt in cfg.accepts:
        if getattr(opt, "network", None) == rede_caip2:
            return opt
    # Rede nao ofertada nesta rota: mantem o comportamento antigo em vez de
    # levantar. Um 402 com a cotacao errada e ruim; um 500 no lugar do 402 e
    # pior -- o cliente nem descobre que a rota e paga.
    return cfg.accepts[0]


def requisitos_da_rota(chave: str, cfg, rede_caip2: str, url_anunciada) -> dict:
    """Fonte UNICA dos requisitos v1 de uma rota.

    Existe para que o corpo do 402 (o que o cliente assina) e a conferencia do
    middleware (o que mandamos ao facilitator) saiam do mesmo lugar. Enquanto
    eram dois trechos parecidos, ja divergiam no `resource`: a producao anuncia
    o caminho de exemplo (`/markets/class/crypto`) e o middleware montava o
    placeholder (`/markets/class/[asset_class]`). Assinatura EIP-3009 nao cobre
    o `resource`, entao talvez nao quebrasse -- "talvez" nao serve aqui.

    `url_anunciada` e a funcao da producao (`_url_anunciada`), injetada em vez
    de reimplementada: uma copia da tabela de exemplos e uma copia que diverge.
    """
    opt = opcao_da_rede(cfg, rede_caip2)
    return requisitos_v1(
        preco_usd=str(opt.price),
        pay_to=opt.pay_to,
        rede_caip2=rede_caip2,
        recurso=url_anunciada(chave.split(" ", 1)[1]),
        descricao=cfg.description or "",
        mime=cfg.mime_type or "application/json",
        timeout_s=opt.max_timeout_seconds or 300,
        scheme=opt.scheme,
    )


# Teto do motivo no corpo do 402. O erro cru do facilitator ja chegou aqui com
# stack colada; corpo de 402 e resposta de API, nao log.
_MOTIVO_MAX = 300

_CAMPO_JSON = {}


def _campo_json(texto: str, campo: str) -> str:
    """Valor de "campo":"..." dentro de um texto que CONTEM json, ou "".

    Regex e nao json.loads porque o que chega nao e um documento: e a mensagem
    de uma excecao com um json embutido no meio, as vezes truncada pelo proprio
    SDK. Um parser exigiria o documento inteiro e devolveria nada justamente
    nos casos em que a informacao ainda esta la."""
    if campo not in _CAMPO_JSON:
        _CAMPO_JSON[campo] = re.compile(
            r'"%s"\s*:\s*"((?:[^"\\]|\\.)*)"' % re.escape(campo))
    m = _CAMPO_JSON[campo].search(texto)
    return m.group(1).replace('\\"', '"') if m else ""


def motivo_legivel(erro) -> str:
    """Uma linha dizendo por que o pagamento foi recusado.

    Entra o erro cru que o SDK repassa (o `str()` da excecao do facilitator,
    com o json do veredito embutido); sai algo sobre o que um agente consegue
    agir -- `invalid_payload (contract call failed: ...)` diz "sua carteira nao
    pagou", que e diferente de "assinatura errada", e so quem recebe o motivo
    sabe qual dos dois consertar.

    Duas regras que este texto nao pode quebrar, porque ele vai no corpo de um
    402 e termina no log de quem chamou:

      - nunca levanta. Recusa que vira 500 troca uma venda perdida por um
        servico quebrado, que e estrago maior do que o que se conserta aqui;
      - nao devolve o endereco do pagador. Ele veio de quem pediu e nao ajuda
        em nada na resposta -- devolver so o espalha.
    """
    if not erro:
        return "Payment required"
    if not isinstance(erro, str):
        try:
            erro = str(erro)
        except Exception:  # noqa: BLE001
            return "Payment required"

    razao = _campo_json(erro, "invalidReason")
    if not razao:
        return erro.strip()[:_MOTIVO_MAX]
    mensagem = _campo_json(erro, "invalidMessage")
    texto = f"payment rejected: {razao}"
    if mensagem:
        texto += f" ({mensagem})"
    return texto[:_MOTIVO_MAX]


def razao_e_pagador(erro) -> tuple[str, str]:
    """(codigo da recusa, endereco do pagador) do veredito cru do facilitator.

    Serve a METRICA, nao a resposta: o vigia precisa saber quem falhou para
    depois perguntar a Base se aquela carteira tinha saldo. Por isso aqui o
    endereco sai, ao contrario de `motivo_legivel`, que devolve texto ao
    cliente e omite o pagador de proposito.

    Campos ausentes viram "" -- quem chama decide o que fazer com o vazio;
    levantar aqui derrubaria o log de um caminho que ja e o de erro."""
    if not erro:
        return "", ""
    if not isinstance(erro, str):
        try:
            erro = str(erro)
        except Exception:  # noqa: BLE001
            return "", ""
    return _campo_json(erro, "invalidReason"), _campo_json(erro, "payer")


def corpo_402_v1(requisitos: dict, erro=None) -> dict:
    """O corpo do 402 em formato v1, a partir dos MESMOS requisitos.

    `erro` so aparece quando o 402 e RECUSA de um pagamento que veio: sem ele o
    texto segue sendo o "Payment required" que o 402 anunciado sempre teve, e o
    cliente assina exatamente o mesmo documento de antes.

    A cotacao vai junto na recusa de proposito. O SDK devolve `{}` nesse
    caminho (`_create_http_response`, com `unpaid_response` vazio), entao o
    cliente v1 -- que so le o corpo -- perde ate o preco e nao tem como refazer
    o pagamento."""
    return {"x402Version": 1, "error": motivo_legivel(erro),
            "accepts": [requisitos]}


def traduz_para_v2(payload_v1: dict, aceito_v2: dict) -> dict:
    """Envelope v2 carregando a MESMA autorizacao assinada do pagamento v1.

    Por que isto e legitimo, e nao um truque: a assinatura EIP-3009 cobre
    `from`, `to`, `value`, `validAfter`, `validBefore` e `nonce` -- e mais
    nada. Nem a string da rede, nem o `resource`. v1 e v2 diferem so no
    envelope (v1 diz scheme/network soltos e a rede legada "base"; v2 embute
    os requisitos em `accepted` com a rede CAIP-2). A autorizacao interna e
    byte a byte a mesma, entao reembrulhar nao invalida nada e nao inventa
    consentimento nenhum: o pagador assinou exatamente esta transferencia.

    Por que reembrulhar em vez de furar o middleware do SDK: assim o caminho
    de verificacao, liquidacao, cabecalho de resposta e contabilidade continua
    sendo o mesmo do v2, que vende desde sempre. Um segundo caminho de
    liquidacao seria um segundo lugar para errar com dinheiro.

    `aceito_v2` tem de vir dos requisitos que o PROPRIO servidor anuncia (o
    cabecalho `payment-required` do 402), nunca de uma copia montada aqui:
    copia montada a mao e copia que diverge, e divergiu uma vez ja."""
    return {"x402Version": 2,
            "payload": payload_v1["payload"],
            "accepted": aceito_v2}


def cabecalho_x_payment(payload: dict) -> bytes:
    """O dict de pagamento como o header X-PAYMENT (base64 de JSON) espera."""
    return base64.b64encode(json.dumps(payload).encode("utf-8"))


def payload_do_cabecalho(cru) -> dict | None:
    """Dict do pagamento a partir do X-PAYMENT (base64 de JSON), ou None.

    Nunca levanta: cabecalho malformado e coisa de scanner, e derrubar a
    requisicao com 500 entregaria um jeito barato de sujar o log."""
    if not cru:
        return None
    if isinstance(cru, str):
        cru = cru.encode("utf-8", "replace")
    if len(cru) > _PAYLOAD_MAX_BYTES:
        return None
    try:
        texto = base64.b64decode(cru + b"===").decode("utf-8", "replace")
        obj = json.loads(texto)
    except (ValueError, TypeError, binascii.Error):
        return None
    return obj if isinstance(obj, dict) else None


def recusa(payload: dict, requisitos: dict) -> str | None:
    """Motivo para nem chamar o facilitator, ou None se o pagamento pode seguir.

    Existe porque o facilitator cobra ida e volta de rede e nao sabe QUAL rota
    estamos vendendo: sem esta conferencia, um pagamento assinado para
    /coverage ($0.001) liberaria /premium ($0.25). O casamento que o middleware
    v2 faz sozinho, aqui e nosso."""
    if not isinstance(payload, dict):
        return "payload ilegivel"
    if payload.get("x402Version") != 1:
        return f"x402Version {payload.get('x402Version')!r} nao e 1"
    if payload.get("scheme") != requisitos["scheme"]:
        return "scheme diferente do anunciado"
    if payload.get("network") != requisitos["network"]:
        return "rede diferente da anunciada"

    interno = payload.get("payload")
    if not isinstance(interno, dict):
        return "payload interno ausente"
    auth = interno.get("authorization")
    if not isinstance(auth, dict):
        return "authorization ausente"

    destino = str(auth.get("to", "")).lower()
    if destino != str(requisitos["payTo"]).lower():
        return "pagamento enderecado a outra carteira"

    try:
        valor = int(auth.get("value"))
    except (TypeError, ValueError):
        return "valor ilegivel"
    if valor < int(requisitos["maxAmountRequired"]):
        return "valor abaixo do preco da rota"
    return None
