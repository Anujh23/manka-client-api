from datetime import date, datetime

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
    prefix: Mapped[str] = mapped_column(String(20))  # e.g. "mcc_3fK9x2Lq", safe to display
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(default=None)
    revoked_at: Mapped[datetime | None] = mapped_column(default=None)


class ApiCall(Base):
    """Usage log: one row per product call, test and live. key_id is empty for calls made from the dashboard."""

    __tablename__ = "api_calls"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True)
    key_id: Mapped[int | None] = mapped_column(ForeignKey("api_keys.id", ondelete="CASCADE"))
    product: Mapped[str] = mapped_column(String(40))
    cost_paise: Mapped[int] = mapped_column(default=0)
    ok: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)


class IfscBranch(Base):
    """Bank branches for the IFSC lookup, loaded with `python -m app.cli load-ifsc` (open Razorpay IFSC dataset)."""

    __tablename__ = "ifsc_branches"

    ifsc: Mapped[str] = mapped_column(String(11), primary_key=True)
    bank: Mapped[str | None] = mapped_column(String(100))
    branch: Mapped[str] = mapped_column(String(120))
    address: Mapped[str] = mapped_column(String(300))
    city: Mapped[str] = mapped_column(String(60))
    district: Mapped[str] = mapped_column(String(60))
    state: Mapped[str] = mapped_column(String(60))
    micr: Mapped[str | None] = mapped_column(String(9))
    upi: Mapped[bool]
    neft: Mapped[bool]
    rtgs: Mapped[bool]
    imps: Mapped[bool]


class Dataset(Base):
    """Which release of a loaded dataset is live, e.g. name="ifsc", version="v2.0.62", as_of=2026-09-01."""

    __tablename__ = "datasets"

    name: Mapped[str] = mapped_column(String(20), primary_key=True)
    version: Mapped[str] = mapped_column(String(40))
    as_of: Mapped[date]


class LocationRequest(Base):
    """Maps API: a one-time link a client sends to someone; opening it and allowing location stores their GPS fix.
    `public_id` (loc_…) is what the client sees; `token` is the secret in the link (/locate/?t=…)."""

    __tablename__ = "location_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = mapped_column(String(20), unique=True)
    token: Mapped[str] = mapped_column(String(64), unique=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True)
    reference: Mapped[str] = mapped_column(String(64))  # the client's own id, e.g. a loan number
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    expires_at: Mapped[datetime]
    lat: Mapped[float | None] = mapped_column(default=None)
    lng: Mapped[float | None] = mapped_column(default=None)
    accuracy_m: Mapped[float | None] = mapped_column(default=None)
    located_at: Mapped[datetime | None] = mapped_column(default=None)  # server time the fix arrived
    ip: Mapped[str | None] = mapped_column(String(45), default=None)
    user_agent: Mapped[str | None] = mapped_column(String(300), default=None)
