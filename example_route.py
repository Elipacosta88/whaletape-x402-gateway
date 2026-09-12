"""The one paid route of this cut-down repository.

In production this is `/coverage`: the cheap probe that tells an agent whether
the paid series is worth buying, backed by the on-chain whale collector. None
of that collector is in this repository, so the handler returns a fixed
payload. The point of the file is the SHAPE: the route is an ordinary FastAPI
handler, and the payment happens entirely in the middleware above it.

Two production rules that are cheap to keep and expensive to rediscover:

1. A route that is not ready must answer >= 400, never 200 with an empty
   body. The x402 middleware does not settle on responses >= 400, so the
   warning is FREE for the buyer. An agent who pays and receives `[]` has no
   way to tell "warming up" from "bad product", and does not come back.
2. The `output_example` published in the 402 must match what the route
   actually returns. The example is the only thing a buyer can inspect before
   paying, and it is a promise.
"""

from __future__ import annotations

from datetime import datetime, timezone

PRICE = "$0.001"

DESCRIPTION = ("Example paid endpoint: returns the coverage summary of the "
               "dataset. Shipped as the reference implementation of an x402 "
               "paid route.")

TAGS = ("x402", "market-data", "ai-agents", "example")

OUTPUT_EXAMPLE = {
    "asset_classes": ["crypto", "equity", "commodity"],
    "markets": 214,
    "oldest_sample": "2026-06-01T00:00:00Z",
    "generated_at": "2026-09-12T00:00:00Z",
}


def payload() -> dict:
    return {**OUTPUT_EXAMPLE,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "note": "static example payload; the production route is backed by "
                    "a live collector"}
