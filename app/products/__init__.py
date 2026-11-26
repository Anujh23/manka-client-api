"""Product APIs. One file per product; add it to PRODUCTS and it gets a /v1 route, a dashboard Try and a price.

Prices are in paise per successful call (₹1 = 100). Only a 200 is charged: bad input (422) is never charged,
and a lookup that finds nothing (404) or a failure (500) is refunded. Every call (API key or the dashboard's Try)
is paid from the client's prepaid balance; with too little balance it gets a 402.
Docs for clients live in the website repo: src/data/api-content.ts (keep paths and fields in sync).
"""
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel
from sqlalchemy.orm import Session

class NotFound(Exception):
    """Raise from a product's run() when there's nothing to return: the caller gets a 404 and is refunded."""


class Rejected(Exception):
    """Raise from a product's run() to refuse the input after charging: the caller gets `status`/`code`/`message`
    and is refunded."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


@dataclass(frozen=True)
class Product:
    name: str  # price key, ApiCall.product, and /account/try/{name}
    title: str  # shown in the ledger, e.g. "API call: PAN check"
    method: Literal["GET", "POST"]
    path: str  # under /v1; GET path params become the input, e.g. "/ifsc/{ifsc}"
    price_paise: int
    input: type[BaseModel]
    run: Callable[[Session, Any, Any], dict]  # (db, validated input, calling Client)


from . import bsa, gstin, ifsc, maps, pan  # noqa: E402  (they import Product from here)

PRODUCTS: list[Product] = [pan.PRODUCT, gstin.PRODUCT, ifsc.PRODUCT, maps.PRODUCT, maps.LOCATE, bsa.PRODUCT]
BY_NAME = {p.name: p for p in PRODUCTS}
PRICES = {p.name: p.price_paise for p in PRODUCTS}
