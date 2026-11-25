"""Dashboard endpoints for a logged-in client (session cookie): balance, ledger, API keys."""
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select

from . import wallet
from .auth import CurrentClient
from .db import DB, utcnow
from .models import Admin, ApiKey, WalletEntry
from .security import new_token, token_hash

router = APIRouter(prefix="/account")

MAX_ACTIVE_KEYS = 10
RECENT = 20


def _ts(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") + "Z" if dt else None  # stored as naive UTC


class KeyOut(BaseModel):
    id: int
    prefix: str
    mode: str
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
    is_admin: bool
    balance_paise: int
    test_balance_paise: int
    keys: list[KeyOut]
    entries: list[EntryOut]


class NewKeyIn(BaseModel):
    mode: Literal["test", "live"]


class NewKeyOut(KeyOut):
    key: str  # shown once, never stored


def _key_out(k: ApiKey) -> dict:
    return dict(id=k.id, prefix=k.prefix, mode=k.mode, created_at=_ts(k.created_at), last_used_at=_ts(k.last_used_at))


@router.get("")
def account(client: CurrentClient, db: DB) -> AccountOut:
    keys = db.scalars(
        select(ApiKey).where(ApiKey.client_id == client.id, ApiKey.revoked_at.is_(None)).order_by(ApiKey.id.desc())
    )
    entries = db.scalars(
        select(WalletEntry).where(WalletEntry.client_id == client.id).order_by(WalletEntry.id.desc()).limit(RECENT)
    )
    out = AccountOut(
        name=client.name,
        email=client.email,
        is_admin=db.get(Admin, client.id) is not None,
        balance_paise=wallet.balance(db, client.id),
        test_balance_paise=wallet.balance(db, client.id, "test"),
        keys=[KeyOut(**_key_out(k)) for k in keys],
        entries=[
            EntryOut(amount_paise=e.amount_paise, balance_after_paise=e.balance_after_paise, kind=e.kind, note=e.note, created_at=_ts(e.created_at))
            for e in entries
        ],
    )
    db.commit()  # first visit creates the wallets (live ₹0, test credit); keep them
    return out


@router.post("/keys")
def create_key(body: NewKeyIn, client: CurrentClient, db: DB) -> NewKeyOut:
    active = db.scalar(select(func.count()).select_from(ApiKey).where(ApiKey.client_id == client.id, ApiKey.revoked_at.is_(None)))
    if active >= MAX_ACTIVE_KEYS:
        raise HTTPException(400, f"You can have up to {MAX_ACTIVE_KEYS} keys. Delete one first.")
    key = f"mcc_{body.mode}_{new_token()}"
    row = ApiKey(client_id=client.id, key_hash=token_hash(key), prefix=key[:13], mode=body.mode)
    db.add(row)
    db.commit()
    return NewKeyOut(key=key, **_key_out(row))


@router.post("/keys/{key_id}/revoke", status_code=204)
def revoke_key(key_id: int, client: CurrentClient, db: DB) -> None:
    row = db.get(ApiKey, key_id)
    if not row or row.client_id != client.id or row.revoked_at:
        raise HTTPException(404, "Key not found.")
    row.revoked_at = utcnow()
    db.commit()
