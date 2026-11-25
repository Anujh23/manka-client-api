from datetime import datetime

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, utcnow


class Client(Base):
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)  # stored lower-case
    name: Mapped[str] = mapped_column(String(120))
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class LoginSession(Base):
    """One row per logged-in browser. Only the SHA-256 of the cookie token is stored."""

    __tablename__ = "sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True)
    expires_at: Mapped[datetime]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class PasswordReset(Base):
    __tablename__ = "password_resets"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True)
    expires_at: Mapped[datetime]
    used: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Admin(Base):
    """Client accounts allowed to use /admin. Grant with `python -m app.cli admin EMAIL`."""

    __tablename__ = "admins"

    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class LoginAttempt(Base):
    """Feeds the brute-force limit in auth.py."""

    __tablename__ = "login_attempts"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(254), index=True)
    ip: Mapped[str] = mapped_column(String(45), index=True)
    ok: Mapped[bool]
    at: Mapped[datetime] = mapped_column(default=utcnow, index=True)


# --- Wallet + API keys (see wallet.py, api.py) ------------------------------------------------
# Money is always integer paise. ₹1 = 100.


class Wallet(Base):
    """Current balance, one row per client. Changed only through wallet.py, which also writes the ledger."""

    __tablename__ = "wallets"

    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), primary_key=True)
    balance_paise: Mapped[int] = mapped_column(default=0)


class TestWallet(Base):
    """Test credit, one row per client, created with products.TEST_CREDIT_PAISE. No ledger: ApiCall logs test usage."""

    __tablename__ = "test_wallets"
    __test__ = False  # the name starts with "Test"; tell pytest it isn't a test class

    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), primary_key=True)
    balance_paise: Mapped[int]


class WalletEntry(Base):
    """Ledger: every credit (+) and debit (-), with the balance right after it."""

    __tablename__ = "wallet_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True)
    amount_paise: Mapped[int]
    balance_after_paise: Mapped[int]
    kind: Mapped[str] = mapped_column(String(20))  # recharge | usage | refund | adjustment
    note: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)


class ApiKey(Base):
    """Only the SHA-256 of the key is stored; the full key is shown once, at creation."""

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    prefix: Mapped[str] = mapped_column(String(20))  # e.g. "mcc_live_3fK9", safe to display
    mode: Mapped[str] = mapped_column(String(4))  # test | live
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(default=None)
    revoked_at: Mapped[datetime | None] = mapped_column(default=None)


class ApiCall(Base):
    """Usage log: one row per /v1 call, test and live."""

    __tablename__ = "api_calls"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True)
    key_id: Mapped[int] = mapped_column(ForeignKey("api_keys.id", ondelete="CASCADE"))
    mode: Mapped[str] = mapped_column(String(4))
    product: Mapped[str] = mapped_column(String(40))
    cost_paise: Mapped[int] = mapped_column(default=0)
    ok: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
