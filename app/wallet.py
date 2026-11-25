"""The only place balances change.

One prepaid balance per client (starts at ₹0). Every change writes a ledger row in the same transaction.
Debits are one atomic UPDATE ... WHERE balance >= amount, so two calls at once can never overdraw.
Callers commit.
"""
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .models import Wallet, WalletEntry


class InsufficientBalance(Exception):
    pass


def _ensure(db: Session, client_id: int) -> None:
    if db.get(Wallet, client_id) is None:
        db.add(Wallet(client_id=client_id, balance_paise=0))
        db.flush()


def balance(db: Session, client_id: int) -> int:
    _ensure(db, client_id)
    return db.scalar(select(Wallet.balance_paise).where(Wallet.client_id == client_id))


def _change(db: Session, client_id: int, paise: int) -> int | None:
    _ensure(db, client_id)
    cond = [Wallet.client_id == client_id] + ([Wallet.balance_paise >= -paise] if paise < 0 else [])
    return db.scalar(update(Wallet).where(*cond).values(balance_paise=Wallet.balance_paise + paise).returning(Wallet.balance_paise))


def credit(db: Session, client_id: int, paise: int, kind: str, note: str = "") -> int:
    """Add money (recharge, refund, adjustment). Returns the new balance."""
    if paise <= 0:
        raise ValueError("Credit must be positive.")
    new = _change(db, client_id, paise)
    db.add(WalletEntry(client_id=client_id, amount_paise=paise, balance_after_paise=new, kind=kind, note=note))
    return new


def debit(db: Session, client_id: int, paise: int, kind: str, note: str = "") -> int:
    """Take money, only if enough is there. Returns the new balance or raises InsufficientBalance."""
    if paise <= 0:
        raise ValueError("Debit must be positive.")
    new = _change(db, client_id, -paise)
    if new is None:
        raise InsufficientBalance
    db.add(WalletEntry(client_id=client_id, amount_paise=-paise, balance_after_paise=new, kind=kind, note=note))
    return new
