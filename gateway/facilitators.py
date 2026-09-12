"""The facilitator clients, and the log wrapper around verify/settle.

Production note kept intact: the CDP facilitator signs a JWT per request, so
the credential must be turned into a `CreateHeadersAuthProvider` -- without it
CDP answers 401 and no route ever settles.

What was removed from the production version: `_record_settle` wrote the sale
into a SQLite ledger and `_log_recusa` fed the traffic tables. Neither exists
here, so both became plain stdout lines. The hook points are the same, so an
operator can put their own accounting in `on_settle` / `on_refusal`.
"""

from __future__ import annotations

import sys

from typing import Any, Callable

from x402.http import FacilitatorConfig, HTTPFacilitatorClient
from x402.schemas import (PaymentRequirements, PaymentRequirementsV1,
                          SettleResponse)

import config
import facilitator_reserva
import robinhood_chain

_CDP_HOST = "api.cdp.coinbase.com"
_CDP_BASE = "/platform/v2/x402"


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _cdp_client() -> HTTPFacilitatorClient:
    from cdp.auth.utils.http import GetAuthHeadersOptions, get_auth_headers
    from x402.http import CreateHeadersAuthProvider

    def _auth(method: str, path: str) -> dict:
        return get_auth_headers(
            GetAuthHeadersOptions(
                api_key_id=config.CDP_API_KEY_ID,
                api_key_secret=config.CDP_API_KEY_SECRET,
                request_method=method,
                request_host=_CDP_HOST,
                request_path=f"{_CDP_BASE}{path}",
            )
        )

    def _headers() -> dict:
        # The provider is called with no arguments and expects a dict of
        # headers per endpoint; CDP JWTs are signed over (method, host, path).
        return {
            "verify": _auth("POST", "/verify"),
            "settle": _auth("POST", "/settle"),
            "supported": _auth("GET", "/supported"),
            "bazaar": _auth("GET", "/discovery/resources"),
        }

    return HTTPFacilitatorClient(
        FacilitatorConfig(url=f"https://{_CDP_HOST}{_CDP_BASE}",
                          auth_provider=CreateHeadersAuthProvider(_headers)))


def on_settle(result: SettleResponse,
              requirements: PaymentRequirements | PaymentRequirementsV1 | None,
              ) -> None:
    """Called once per successful settlement. Put your accounting here.

    The amount charged exists ONLY in `requirements`, never in the facilitator
    response -- that mistake cost a day of wrong numbers in production."""
    _log(f"[x402 settle] {result}")


def on_refusal(erro: Exception) -> None:
    """Called when the facilitator refuses a payment that was actually sent."""
    _log(f"[x402 refusal] {erro!r}")


def instrumentar(cliente: Any) -> Any:
    """Wrap ONE facilitator client so every verify/settle is logged.

    Every client that goes into the server's list must pass through here: in
    production, a sale through the second facilitator (Naven) was invisible to
    the metrics and to the watchdog until this stopped being a one-off."""
    for nome in ("verify", "settle", "verify_from_bytes", "settle_from_bytes"):
        if not hasattr(cliente, nome):
            continue

        def _make(fn: Callable[..., Any], name: str) -> Callable[..., Any]:
            async def _logged(*a, **kw):
                try:
                    result = await fn(*a, **kw)
                    _log(f"[x402 {name}] {result}")
                    if name.startswith("settle"):
                        reqs = kw.get("requirements") or (a[1] if len(a) > 1 else None)
                        on_settle(result, reqs)
                    return result
                except Exception as e:  # noqa: BLE001 -- logging is the point
                    _log(f"[x402 {name}] EXCEPTION: {e!r}")
                    if name.startswith("verify"):
                        on_refusal(e)
                    raise
            return _logged

        setattr(cliente, nome, _make(getattr(cliente, nome), nome))
    return cliente


def construir() -> list[HTTPFacilitatorClient]:
    """The facilitator list, in the order the SDK should consult it.

    The SDK gives precedence to the FIRST facilitator that supports a given
    network, so Naven only ever receives what CDP does not cover."""
    if config.FACILITATOR_URL:
        # Explicit override: any facilitator that needs no authentication.
        # PayAI, for instance, serves Base and Arbitrum One in v2 exact, so a
        # deployment without CDP credentials can still sell on those two.
        # It does NOT cover Solana -- that one is CDP-only today.
        primario = HTTPFacilitatorClient(FacilitatorConfig(url=config.FACILITATOR_URL))
    elif config.CDP_API_KEY_ID and config.CDP_API_KEY_SECRET:
        primario = _cdp_client()
        reservas = ([HTTPFacilitatorClient(
                        FacilitatorConfig(url=config.FACILITATOR_RESERVA_URL))]
                    if config.FACILITATOR_RESERVA_URL else [])
        primario = facilitator_reserva.encadear(primario, reservas)
    else:
        # No credentials: the free public testnet facilitator.
        primario = HTTPFacilitatorClient(
            FacilitatorConfig(url="https://x402.org/facilitator"))

    clientes = [instrumentar(primario)]
    if config.PAY_TO_ROBINHOOD:
        clientes.append(instrumentar(robinhood_chain.cliente_naven()))
    return clientes
