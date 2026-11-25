"""Dashboard endpoints for a logged-in client (session cookie): balance, ledger, API keys, Try.

One API key per client: generating a new one revokes the old one.
"""
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body
from pydantic import BaseModel
from sqlalchemy import select, update

from . import api, wallet
from .auth import CurrentClient
from .db import DB, utcnow
from .models import ApiKey, LocationRequest, WalletEntry
from .products import BY_NAME, PRICES, maps
from .security import new_token, token_hash

router = APIRouter(prefix="/account")

RECENT = 20


def _ts(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") + "Z" if dt else None  # stored as naive UTC


class KeyOut(BaseModel):
    id: int
    prefix: str
    created_at: str
    last_used_at: str | None


class EntryOut(BaseModel):
    amount_paise: int
    balance_after_paise: int
    kind: str
    note: str
    created_at: str


class AccountOut(BaseModel):
    name: str
    email: str
    balance_paise: int
    prices: dict[str, int]  # paise per call, by product name
    key: KeyOut | None
    entries: list[EntryOut]


class NewKeyOut(KeyOut):
    key: str  # shown once, never stored


def _key_out(k: ApiKey) -> dict:
    return dict(id=k.id, prefix=k.prefix, created_at=_ts(k.created_at), last_used_at=_ts(k.last_used_at))


@router.get("")
def account(client: CurrentClient, db: DB) -> AccountOut:
    key = db.scalar(select(ApiKey).where(ApiKey.client_id == client.id, ApiKey.revoked_at.is_(None)).order_by(ApiKey.id.desc()))
    entries = db.scalars(
        select(WalletEntry).where(WalletEntry.client_id == client.id).order_by(WalletEntry.id.desc()).limit(RECENT)
    )
    out = AccountOut(
        name=client.name,
        email=client.email,
        balance_paise=wallet.balance(db, client.id),
        prices=PRICES,
        key=KeyOut(**_key_out(key)) if key else None,
        entries=[
            EntryOut(amount_paise=e.amount_paise, balance_after_paise=e.balance_after_paise, kind=e.kind, note=e.note, created_at=_ts(e.created_at))
            for e in entries
        ],
    )
    db.commit()  # first visit creates the wallet (₹0); keep it
    return out


@router.post("/keys")
def create_key(client: CurrentClient, db: DB) -> NewKeyOut:
    """Generate (or regenerate) the client's API key. The previous one stops working at once."""
    db.execute(update(ApiKey).where(ApiKey.client_id == client.id, ApiKey.revoked_at.is_(None)).values(revoked_at=utcnow()))
    key = f"mcc_{new_token()}"
    row = ApiKey(client_id=client.id, key_hash=token_hash(key), prefix=key[:12])
    db.add(row)
    db.commit()
    return NewKeyOut(key=key, **_key_out(row))


@router.post("/try/{name}")
def try_product(name: str, client: CurrentClient, db: DB, body: Annotated[Any, Body()] = None) -> dict:
    """Run a product from the dashboard: same result and charge (from the balance) as /v1."""
    product = BY_NAME.get(name)
    if product is None:
        raise api.ApiError(404, "not_found", "No such API.")
    return api.execute(db, product, body, api.Caller(client=client, key=None))


@router.get("/locations")
def locations(client: CurrentClient, db: DB) -> list[dict]:
    """Maps API requests for the dashboard, newest first."""
    rows = db.scalars(
        select(LocationRequest).where(LocationRequest.client_id == client.id).order_by(LocationRequest.id.desc()).limit(RECENT)
    )
    return [maps.view(r) for r in rows]
