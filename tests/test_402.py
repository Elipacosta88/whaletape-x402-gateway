"""Tests for the public cut of the WhaleTape x402 gateway.

Run with:
    cd /home/ubuntu/whaletape-x402-gateway-public && \
    /home/ubuntu/venv-gateway-public/bin/python -m unittest -v

Why unittest and not pytest: pytest is not installed in
/home/ubuntu/venv-gateway-public (checked before writing this file).

Why the facilitator is mocked: the real primary facilitator (either the
free public https://x402.org/facilitator, or CDP with credentials) is
consulted over the network the first time a paid route is hit
(`x402_http_server_base.py:initialize` calls `get_supported()` and raises
`RouteConfigurationError` for any network the facilitator does not list).
Hitting the network here would make the tests flaky and slow. The fake
`get_supported` below advertises the four EVM/SVM networks this repository
configures, so `initialize()` succeeds without a socket. Algorand never
goes through this facilitator: `caixa_algorand.py` builds its `accepts`
entry locally (`ALGORAND_GATEWAY_URL` unset in these tests), so it needs no
mock.
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
GATEWAY_DIR = REPO_ROOT / "gateway"

# ---------------------------------------------------------------- fixtures --
# Fictitious but well-formed addresses: no real funds or identity behind any
# of them. EVM ones are the same address repeated (0x11...11): the gateway
# never validates that different networks use different addresses, and reusing
# one keeps the fixture short.
ENV = {
    "PAY_TO_ADDRESS": "0x1111111111111111111111111111111111111111",
    "NETWORK": "eip155:8453",
    "PAY_TO_SOLANA": "DummySolanaAddr11111111111111111111111111",
    "SOLANA_NETWORK": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
    "PAY_TO_ARBITRUM": "0x1111111111111111111111111111111111111111",
    "ARBITRUM_NETWORK": "eip155:42161",
    "PAY_TO_ROBINHOOD": "0x1111111111111111111111111111111111111111",
    "ROBINHOOD_NETWORK": "eip155:4663",
    "PAY_TO_ALGORAND": "DUMMYALGORANDADDRESSXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX",
    "ALGORAND_GATEWAY_URL": "",  # local static Algorand entry, no network call
    "X402_REDE_PRIMEIRA": "base",  # deterministic order for the assertions below
    "SITE": "https://whaletape.xyz",
}

_FACILITATOR_NETWORKS = (
    "eip155:8453",
    "eip155:42161",
    "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
    "eip155:4663",
)


def _fake_get_supported(self):
    """Stand-in for HTTPFacilitatorClient.get_supported(): no network call.

    Advertises `exact` support for exactly the networks this repository's
    .env configures, matching what the real CDP/testnet facilitator supports
    for those networks in production.
    """
    from x402.schemas import SupportedKind, SupportedResponse

    kinds = [
        SupportedKind(
            x402_version=2,
            scheme="exact",
            network=rede,
            extra={"feePayer": "CKPKJWNdJEqa81x7CkZ14BVPiY6y16Sxs7owznqtWYp5"}
            if rede.startswith("solana:") else None,
        )
        for rede in _FACILITATOR_NETWORKS
    ]
    return SupportedResponse(kinds=kinds)


def _b64_json(cabecalho: str) -> dict:
    return json.loads(base64.b64decode(cabecalho))


def setUpModule():
    """Builds ONE FastAPI app for the whole file: `config.py` reads `os.environ`
    at import time and raises `SystemExit` without `PAY_TO_ADDRESS`, so the
    environment has to be in place before `gateway.config` (and therefore
    `main`) is imported for the first time in the process."""
    global main, payment_options, config, robinhood_chain, v1_payments

    os.environ.update(ENV)
    sys.path.insert(0, str(GATEWAY_DIR))

    patcher = patch("x402.http.HTTPFacilitatorClient.get_supported",
                    _fake_get_supported)
    patcher.start()
    setUpModule.patcher = patcher  # keep alive; stopped in tearDownModule

    import config  # noqa: F401
    import main  # noqa: F401
    import payment_options  # noqa: F401
    import robinhood_chain  # noqa: F401
    import v1_payments  # noqa: F401


def tearDownModule():
    setUpModule.patcher.stop()


# ------------------------------------------------------------- test cases --
class TestOpcoesDePagamento(unittest.TestCase):
    """`opcoes_de_pagamento` (pure function): the four EVM/SVM networks."""

    def setUp(self):
        self.opcoes = payment_options.opcoes_de_pagamento("$0.001")
        self.por_rede = {o.network: o for o in self.opcoes}

    def test_emits_exactly_base_solana_arbitrum_robinhood(self):
        self.assertEqual(
            set(self.por_rede),
            {"eip155:8453", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
             "eip155:42161", "eip155:4663"},
        )

    def test_pay_to_comes_from_the_environment(self):
        self.assertEqual(self.por_rede["eip155:8453"].pay_to,
                         ENV["PAY_TO_ADDRESS"])
        self.assertEqual(self.por_rede["solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"].pay_to,
                         ENV["PAY_TO_SOLANA"])
        self.assertEqual(self.por_rede["eip155:42161"].pay_to,
                         ENV["PAY_TO_ARBITRUM"])
        self.assertEqual(self.por_rede["eip155:4663"].pay_to,
                         ENV["PAY_TO_ROBINHOOD"])

    def test_robinhood_uses_the_usdg_eip712_domain(self):
        # The domain is not on the PaymentOption itself: it comes from the
        # money parser registered for eip155:4663 (robinhood_chain.parser_usdg).
        self.assertEqual(robinhood_chain.USDG_EIP712,
                         {"name": "Global Dollar", "version": "1"})
        # The parser receives the amount already stripped of the "$" (the
        # lib's own money-string handling happens before a custom parser is
        # tried); "0.001" -> 1000 units at 6 decimals.
        montante = robinhood_chain.parser_usdg("0.001", "eip155:4663")
        self.assertIsNotNone(montante)
        self.assertEqual(montante.amount, "1000")
        self.assertEqual(montante.asset, robinhood_chain.USDG)
        self.assertEqual(montante.extra, {"name": "Global Dollar", "version": "1"})

    def test_robinhood_parser_ignores_other_networks(self):
        self.assertIsNone(robinhood_chain.parser_usdg("0.001", "eip155:8453"))

    def test_algorand_is_not_part_of_opcoes_de_pagamento(self):
        # Algorand is spliced in separately (accepts_algorand_estatico /
        # caixa_algorand), never through opcoes_de_pagamento: this process
        # cannot verify or settle it. See README.md, section "Algorand".
        self.assertNotIn("algorand:wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8=",
                         self.por_rede)


class TestAccepts402(unittest.TestCase):
    """GET /coverage without payment: the v2 quote (`payment-required` header)."""

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        cls.client = TestClient(main.app)

    def test_returns_402(self):
        r = self.client.get("/coverage")
        self.assertEqual(r.status_code, 402)

    def test_v2_accepts_lists_the_five_networks(self):
        r = self.client.get("/coverage")
        cabecalho = r.headers.get("payment-required")
        self.assertIsNotNone(cabecalho)
        requisitos = _b64_json(cabecalho)
        redes = {a["network"] for a in requisitos["accepts"]}
        self.assertEqual(
            redes,
            {"eip155:8453", "eip155:42161", "eip155:4663",
             "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
             "algorand:wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8="},
        )

    def test_algorand_entry_carries_the_configured_pay_to_and_asset(self):
        r = self.client.get("/coverage")
        requisitos = _b64_json(r.headers.get("payment-required"))
        algo = next(a for a in requisitos["accepts"]
                    if a["network"].startswith("algorand:"))
        self.assertEqual(algo["payTo"], ENV["PAY_TO_ALGORAND"])
        self.assertEqual(algo["asset"], "31566704")


class TestV1Body(unittest.TestCase):
    """The v1 body of the same 402: `accepts` is Base-only, Algorand rides in
    `alternatives` -- a legacy x402 v1 client validates every `accepts` entry
    against a closed enum of networks and rejects an unknown one before it
    ever pays (see README.md and gateway/caixa_algorand.py)."""

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        cls.client = TestClient(main.app)

    def test_accepts_has_only_base(self):
        r = self.client.get("/coverage")
        corpo = r.json()
        self.assertEqual(corpo["x402Version"], 1)
        self.assertEqual(len(corpo["accepts"]), 1)
        self.assertEqual(corpo["accepts"][0]["network"], "base")
        self.assertEqual(corpo["accepts"][0]["payTo"], ENV["PAY_TO_ADDRESS"])

    def test_algorand_is_only_in_alternatives(self):
        r = self.client.get("/coverage")
        corpo = r.json()
        redes_accepts = {a["network"] for a in corpo["accepts"]}
        self.assertNotIn("algorand:wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8=",
                         redes_accepts)
        redes_alt = {a["network"] for a in corpo.get("alternatives", [])}
        self.assertIn("algorand:wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8=",
                      redes_alt)

    def test_v1_requisitos_match_the_advertised_body(self):
        # requisitos_da_rota is the SAME function that builds the advertised
        # body and the one the v1 middleware checks a payment against -- they
        # must never diverge (see v1_payments.py docstring).
        cfg = main.routes[main.ROUTE_KEY]
        requisitos = v1_payments.requisitos_da_rota(
            main.ROUTE_KEY, cfg, config.NETWORK, payment_options.url_anunciada)
        r = self.client.get("/coverage")
        self.assertEqual(requisitos, r.json()["accepts"][0])


class TestIndex(unittest.TestCase):
    """GET / -- free, lists the paid route and the networks."""

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        cls.client = TestClient(main.app)

    def test_returns_200(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)

    def test_lists_the_configured_networks(self):
        r = self.client.get("/")
        corpo = r.json()
        self.assertEqual(
            set(corpo["networks"]),
            {"eip155:8453", "eip155:42161", "eip155:4663",
             "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
             "algorand:wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8="},
        )
        self.assertEqual(corpo["x402_versions"], [1, 2])
        self.assertEqual(corpo["paid_routes"][0]["path"], "/coverage")


class TestEnvExampleHasNoRealSecrets(unittest.TestCase):
    """.env.example must stay a template: no SECRET/KEY/PRIVATE variable may
    carry a filled-in value, only an empty one or a placeholder that starts
    with `0x000...0` (the documented placeholder address)."""

    _SENSITIVE_NAME = re.compile(r"(SECRET|KEY|PRIVATE)", re.IGNORECASE)
    _PLACEHOLDER_ADDRESS = re.compile(r"^0x0+$")

    def test_no_sensitive_variable_has_a_real_looking_value(self):
        caminho = REPO_ROOT / ".env.example"
        self.assertTrue(caminho.is_file(), f"missing {caminho}")
        for numero, linha in enumerate(caminho.read_text().splitlines(), start=1):
            linha_limpa = linha.strip()
            if not linha_limpa or linha_limpa.startswith("#") or "=" not in linha_limpa:
                continue
            nome, _, valor = linha_limpa.partition("=")
            nome, valor = nome.strip(), valor.strip()
            if not self._SENSITIVE_NAME.search(nome):
                continue
            if valor == "" or self._PLACEHOLDER_ADDRESS.match(valor):
                continue
            self.fail(f"{caminho}:{numero} {nome} looks filled in: {valor!r}")


if __name__ == "__main__":
    unittest.main()
