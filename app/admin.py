"""Admin endpoints (/admin): manage client accounts from the browser instead of the CLI.

An admin is a normal client account with a row in `admins` (grant: `python -m app.cli admin EMAIL`).
"Delete" is disable: login and API keys stop working, but the ledger and usage history stay
(needed for accounts/GST and disputes) and the client can be enabled again.
"""
import re
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from . import wallet
from .auth import CurrentClient
from .db import DB, utcnow
from .models import Admin, Client, LoginSession, TestWallet, Wallet
from .products import TEST_CREDIT_PAISE
from .security import MIN_PASSWORD, hash_password

router = APIRouter(prefix="/admin")

EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_WALLET_CHANGE = Decimal("1000000")  # ₹10 lakh per entry: guards against a typo with extra zeros


def current_admin(client: CurrentClient, db: DB) -> Client:
    if db.get(Admin, client.id) is None:
        raise HTTPException(403, "Admins only.")
    return client


AdminUser = Annotated[Client, Depends(current_admin)]


class ClientRow(BaseModel):
    id: int
    name: str
    email: str
    is_active: bool
    is_admin: bool
    balance_paise: int
    test_balance_paise: int
    created_at: str


class NewClientIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: str = Field(max_length=254)
    password: str = Field(min_length=MIN_PASSWORD, max_length=256)


class EditClientIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: str = Field(max_length=254)


class PasswordIn(BaseModel):
    password: str = Field(min_length=MIN_PASSWORD, max_length=256)


class WalletIn(BaseModel):
    action: Literal["credit", "debit"]
    rupees: Decimal = Field(gt=0, le=MAX_WALLET_CHANGE, decimal_places=2)
    note: str = Field(min_length=1, max_length=200)


def _email(raw: str, db: Session, except_id: int | None = None) -> str:
    email = raw.strip().lower()
    if not EMAIL.match(email):
        raise HTTPException(400, "Enter a valid email.")
    taken = db.scalar(select(Client.id).where(Client.email == email))
    if taken is not None and taken != except_id:
        raise HTTPException(400, "This email already has an account.")
    return email


def _client(db: Session, client_id: int) -> Client:
    client = db.get(Client, client_id)
    if client is None:
        raise HTTPException(404, "Client not found.")
    return client


def _rows(db: Session, only_id: int | None = None) -> list[ClientRow]:
    """One query for the list: balances via outer joins, so listing never creates wallet rows."""
    q = (
        select(
            Client,
            Admin.client_id.is_not(None),
            func.coalesce(Wallet.balance_paise, 0),
            func.coalesce(TestWallet.balance_paise, TEST_CREDIT_PAISE),
        )
        .outerjoin(Admin, Admin.client_id == Client.id)
        .outerjoin(Wallet, Wallet.client_id == Client.id)
        .outerjoin(TestWallet, TestWallet.client_id == Client.id)
        .order_by(Client.id.desc())
    )
    if only_id is not None:
        q = q.where(Client.id == only_id)
    return [
        ClientRow(
            id=c.id, name=c.name, email=c.email, is_active=c.is_active, is_admin=bool(is_admin),
            balance_paise=bal, test_balance_paise=test, created_at=c.created_at.isoformat(timespec="seconds") + "Z",
        )
        for c, is_admin, bal, test in db.execute(q)
    ]


def _logout_everywhere(db: Session, client_id: int) -> None:
    db.execute(delete(LoginSession).where(LoginSession.client_id == client_id))


@router.get("/clients")
def list_clients(_: AdminUser, db: DB) -> list[ClientRow]:
    return _rows(db)


@router.post("/clients")
def create_client(body: NewClientIn, _: AdminUser, db: DB) -> ClientRow:
    client = Client(email=_email(body.email, db), name=body.name.strip(), password_hash=hash_password(body.password))
    db.add(client)
    db.commit()
    return _rows(db, client.id)[0]


@router.post("/clients/{client_id}")
def edit_client(client_id: int, body: EditClientIn, _: AdminUser, db: DB) -> ClientRow:
    client = _client(db, client_id)
    client.email = _email(body.email, db, except_id=client.id)
    client.name = body.name.strip()
    db.commit()
    return _rows(db, client.id)[0]


@router.post("/clients/{client_id}/password", status_code=204)
def set_password(client_id: int, body: PasswordIn, _: AdminUser, db: DB) -> None:
    client = _client(db, client_id)
    client.password_hash = hash_password(body.password)
    _logout_everywhere(db, client.id)
    db.commit()


@router.post("/clients/{client_id}/disable", status_code=204)
def disable_client(client_id: int, admin: AdminUser, db: DB) -> None:
    if client_id == admin.id:
        raise HTTPException(400, "You can't disable your own account.")
    client = _client(db, client_id)
    client.is_active = False  # login and every API key stop working (both check is_active)
    _logout_everywhere(db, client.id)
    db.commit()


@router.post("/clients/{client_id}/enable", status_code=204)
def enable_client(client_id: int, _: AdminUser, db: DB) -> None:
    _client(db, client_id).is_active = True
    db.commit()


@router.post("/clients/{client_id}/wallet")
def change_wallet(client_id: int, body: WalletIn, admin: AdminUser, db: DB) -> ClientRow:
    client = _client(db, client_id)
    paise = int(body.rupees * 100)
    note = f"{body.note.strip()} (by {admin.email})"[:200]
    if body.action == "credit":
        wallet.credit(db, client.id, paise, "recharge", note)
    else:
        try:
            wallet.debit(db, client.id, paise, "adjustment", note)
        except wallet.InsufficientBalance:
            db.rollback()
            raise HTTPException(400, "That's more than the client's balance.") from None
    db.commit()
    return _rows(db, client.id)[0]
