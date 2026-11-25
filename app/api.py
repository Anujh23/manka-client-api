"""Public product API (/v1). Callers authenticate with `Authorization: Bearer mcc_test_… | mcc_live_…`.

Calls are charged BEFORE the product runs (so two calls can't spend the same rupee) and refunded
automatically if the product fails. Live keys pay from the wallet, test keys from the one-time test credit.
"""
from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select

from . import wallet
from .db import DB, utcnow
from .models import ApiCall, ApiKey, Client
from .products import PRICES
from .security import token_hash

router = APIRouter(prefix="/v1")


@dataclass
class Caller:
    client: Client
    mode: str  # test | live


def _key(authorization: str | None) -> str:
    scheme, _, key = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not key.strip():
        raise HTTPException(401, "Missing API key. Send 'Authorization: Bearer <key>'.")
    return key.strip()


def charged(product: str):
    if product not in PRICES:
        raise KeyError(f"No price for product {product!r} in products.py")

    def dependency(db: DB, authorization: Annotated[str | None, Header()] = None):
        key = db.scalar(select(ApiKey).where(ApiKey.key_hash == token_hash(_key(authorization)), ApiKey.revoked_at.is_(None)))
        client = db.get(Client, key.client_id) if key else None
        if not key or not client or not client.is_active:
            raise HTTPException(401, "Invalid API key.")

        cost = PRICES[product]
        if cost:
            try:
                wallet.debit(db, client.id, cost, "usage", product, mode=key.mode)
            except wallet.InsufficientBalance:
                db.rollback()
                detail = "Insufficient balance. Recharge to continue." if key.mode == "live" else "Test credit used up. Use a live key to continue."
                raise HTTPException(402, detail) from None
        call = ApiCall(client_id=client.id, key_id=key.id, mode=key.mode, product=product, cost_paise=cost)
        db.add(call)
        key.last_used_at = utcnow()
        db.commit()

        try:
            yield Caller(client=client, mode=key.mode)
        except Exception:
            db.rollback()
            call.ok = False
            if cost:
                wallet.credit(db, client.id, cost, "refund", product, mode=key.mode)
            db.commit()
            raise

    return dependency


@router.get("/ping")
def ping(caller: Annotated[Caller, Depends(charged("ping"))]) -> dict:
    """Lets a client check their key works."""
    return {"ok": True, "mode": caller.mode, "client": caller.client.name}
