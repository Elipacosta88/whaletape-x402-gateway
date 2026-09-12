"""Caminho-modelo do catalogo e o exemplo que funciona no lugar dele.

Por que este modulo existe
==========================
O Bazaar indexa a rota parametrizada como se fosse URL. O diretorio publica
`/markets/class/:asset_class`, o agente copia aquilo e chama o caminho
literal: recebe o 402 normal, monta o pagamento, a assinatura passa na
verificacao -- e ai o FastAPI nao tem rota pra aquele caminho e devolve 404. O
middleware nao liquida em resposta >= 400, entao ninguem paga e ninguem
recebe. A venda evapora, e do lado de fora nada distingue essa rota de uma boa
ate o pagamento ja estar montado.

Aconteceu em 25/08/2026 as 18:39 UTC (pagador 0xC533Bf52, /markets/class/
:asset_class), e o api.log tinha 3.749 requisicoes a caminho com placeholder
literal esperando pra repetir a cena.

A tabela vive aqui porque tres lugares precisavam dela e cada um tinha a
propria copia: a API (que anuncia e que atende), o verificar_compras.py e o
inscrever_x402list.py. Copia divergente aqui significa venda perdida.
"""

from __future__ import annotations

# Chave: rota do catalogo, na forma com colchete. Valor: caminho real,
# escolhido por ser o mais obvio pra quem esta avaliando o produto.
EXEMPLOS = {
    "/markets/class/[asset_class]": "/markets/class/crypto",
    "/history/[symbol]": "/history/BTC",
    "/coverage/[symbol]": "/coverage/BTC",
    "/whale/[address]": "/whale/0x5b5d51203a0f9079f8aeb098a6523a13f298c060",
    # Merchant real do diretorio do facilitator: exemplo que devolve dado de
    # verdade quando ha ciclo, em vez de um id inventado que sempre da vazio.
    "/index/merchant/[id]": "/index/merchant/api.syraa.fun",
}

_ABERTURAS = ("[", "{", ":")


def _normalizar(caminho: str) -> str:
    """Reduz as tres notacoes de parametro a uma so, pra comparar.

    O mesmo parametro aparece como [x] na chave do catalogo, :x no que o
    Bazaar publica e {x} no decorador do FastAPI. Sao a mesma coisa e o
    comprador pode chegar com qualquer uma."""
    partes = []
    for parte in (caminho or "").strip().lower().rstrip("/").split("/"):
        if parte.startswith(_ABERTURAS) or (parte.startswith("[") and parte.endswith("]")):
            partes.append("*")
        else:
            partes.append(parte)
    return "/".join(partes)


_POR_FORMA = {_normalizar(k): v for k, v in EXEMPLOS.items()}


def url_chamavel(site: str, caminho: str, prefixo: str = "") -> str:
    """URL que um agente consegue chamar de verdade, para o `resource`.

    O caminho-modelo (`/coverage/[symbol]`) e a IDENTIDADE da rota e continua
    valendo no campo `path`. Mas quem indexa o manifesto -- Bazaar, x402list,
    agente autonomo -- copia o `resource` literal e monta pagamento para uma
    URL que devolve 400. Agente nenhum sabe substituir placeholder.

    Foi o incidente de 25/08/2026, e em 09/09/2026 a auditoria achou os mesmos
    10 placeholders ainda publicados: a API viva ja corrigia na RESPOSTA, mas
    a vitrine seguia anunciando o modelo. Modelo desconhecido sai como veio --
    `concreto()` devolve None em vez de chutar, e chute e pior que placeholder."""
    return f"{site}{prefixo}{concreto(caminho) or caminho}"


def concreto(caminho: str | None) -> str | None:
    """Caminho real equivalente, ou None quando nao e um modelo conhecido.

    None em vez de chute: sugerir caminho errado a um agente e pior do que nao
    sugerir nada -- ele paga de novo, erra de novo e conclui que o produto nao
    funciona."""
    if not caminho:
        return None
    return _POR_FORMA.get(_normalizar(caminho))
