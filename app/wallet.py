"""The only place balances change.

Two wallets per client: "live" (real money; every change writes a ledger row in the same transaction)
and "test" (a one-time test credit; no ledger, ApiCall records test usage).
Debits are one atomic UPDATE ... WHERE balance >= amount, so two calls at once can never overdraw.
Callers commit.
"""
from typing import Literal

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .models import TestWallet, Wallet, WalletEntry
from .products import TEST_CREDIT_PAISE

Mode = Literal["live", "test"]


class InsufficientBalance(Exception):
    pass


def _table(mode: Mode):
    return Wallet if mode == "live" else TestWallet


def _ensure(db: Session, client_id: int, mode: Mode) -> None:
    table = _table(mode)
    if db.get(table, client_id) is None:
        db.add(table(client_id=client_id, balance_paise=0 if mode == "live" else TEST_CREDIT_PAISE))
        db.flush()


def balance(db: Session, client_id: int, mode: Mode = "live") -> int:
    _ensure(db, client_id, mode)
    table = _table(mode)
    return db.scalar(select(table.balance_paise).where(table.client_id == client_id))


def _change(db: Session, client_id: int, paise: int, mode: Mode) -> int | None:
    table = _table(mode)
    _ensure(db, client_id, mode)
    cond = [table.client_id == client_id] + ([table.balance_paise >= -paise] if paise < 0 else [])
    return db.scalar(update(table).where(*cond).values(balance_paise=table.balance_paise + paise).returning(table.balance_paise))


def credit(db: Session, client_id: int, paise: int, kind: str, note: str = "", mode: Mode = "live") -> int:
    """Add money (recharge, refund, adjustment). Returns the new balance."""
    if paise <= 0:
        raise ValueError("Credit must be positive.")
    new = _change(db, client_id, paise, mode)
    if mode == "live":
        db.add(WalletEntry(client_id=client_id, amount_paise=paise, balance_after_paise=new, kind=kind, note=note))
    return new


def debit(db: Session, client_id: int, paise: int, kind: str, note: str = "", mode: Mode = "live") -> int:
    """Take money, only if enough is there. Returns the new balance or raises InsufficientBalance."""
    if paise <= 0:
        raise ValueError("Debit must be positive.")
    new = _change(db, client_id, -paise, mode)
    if new is None:
        raise InsufficientBalance
    if mode == "live":
        db.add(WalletEntry(client_id=client_id, amount_paise=-paise, balance_after_paise=new, kind=kind, note=note))
    return new
