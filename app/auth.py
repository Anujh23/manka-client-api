"""Client login: email + password, cookie session, password reset by email."""
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Cookie, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select

from . import mailer
from .config import get_settings
from .db import DB, utcnow
from .models import Admin, Client, LoginAttempt, LoginSession, PasswordReset
from .security import MIN_PASSWORD, hash_password, needs_rehash, new_token, token_hash, verify_password

router = APIRouter(prefix="/auth")

COOKIE = "session"
WINDOW = timedelta(minutes=15)
MAX_FAILS_PER_EMAIL = 5
MAX_FAILS_PER_IP = 30
RESET_TTL = timedelta(hours=1)
RESET_COOLDOWN = timedelta(minutes=2)


class LoginIn(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=256)


class ForgotIn(BaseModel):
    email: str = Field(max_length=254)


class ResetIn(BaseModel):
    token: str = Field(max_length=128)
    password: str = Field(min_length=MIN_PASSWORD, max_length=256)


class ClientOut(BaseModel):
    name: str
    email: str


class MeOut(ClientOut):
    is_admin: bool  # shows the Admin item in the account menu


def _email(raw: str) -> str:
    return raw.strip().lower()


def _ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def current_client(db: DB, session: Annotated[str | None, Cookie(alias=COOKIE)] = None) -> Client:
    if session:
        row = db.get(LoginSession, token_hash(session))
        if row and row.expires_at > utcnow():
            client = db.get(Client, row.client_id)
            if client and client.is_active:
                return client
    raise HTTPException(401, "Not logged in.")


CurrentClient = Annotated[Client, Depends(current_client)]


@router.post("/login")
def login(body: LoginIn, request: Request, response: Response, db: DB) -> ClientOut:
    email, ip, since = _email(body.email), _ip(request), utcnow() - WINDOW
    fails = select(func.count()).select_from(LoginAttempt).where(LoginAttempt.ok.is_(False), LoginAttempt.at > since)
    if (
        db.scalar(fails.where(LoginAttempt.email == email)) >= MAX_FAILS_PER_EMAIL
        or db.scalar(fails.where(LoginAttempt.ip == ip)) >= MAX_FAILS_PER_IP
    ):
        raise HTTPException(429, "Too many attempts. Try again in 15 minutes.")

    client = db.scalar(select(Client).where(Client.email == email))
    ok = verify_password(client.password_hash if client else None, body.password) and client.is_active
    db.add(LoginAttempt(email=email, ip=ip, ok=ok))
    if not ok:
        db.commit()
        raise HTTPException(401, "Wrong email or password.")

    if needs_rehash(client.password_hash):
        client.password_hash = hash_password(body.password)
    s = get_settings()
    token = new_token()
    db.add(LoginSession(token_hash=token_hash(token), client_id=client.id, expires_at=utcnow() + timedelta(days=s.session_days)))
    db.commit()

    response.set_cookie(
        COOKIE, token,
        max_age=s.session_days * 86400, httponly=True, secure=s.cookie_secure, samesite="lax", path="/",
    )
    return ClientOut(name=client.name, email=client.email)


@router.post("/logout", status_code=204)
def logout(response: Response, db: DB, session: Annotated[str | None, Cookie(alias=COOKIE)] = None) -> None:
    if session:
        db.execute(delete(LoginSession).where(LoginSession.token_hash == token_hash(session)))
        db.commit()
    s = get_settings()
    response.delete_cookie(COOKIE, httponly=True, secure=s.cookie_secure, samesite="lax", path="/")


@router.get("/me")
def me(client: CurrentClient, db: DB) -> MeOut:
    return MeOut(name=client.name, email=client.email, is_admin=db.get(Admin, client.id) is not None)


@router.post("/forgot", status_code=204)
def forgot(body: ForgotIn, tasks: BackgroundTasks, db: DB) -> None:
    """Always 204, so the response never reveals whether an email has an account."""
    client = db.scalar(select(Client).where(Client.email == _email(body.email), Client.is_active.is_(True)))
    if not client:
        return
    recent = db.scalar(
        select(PasswordReset).where(PasswordReset.client_id == client.id, PasswordReset.created_at > utcnow() - RESET_COOLDOWN)
    )
    if recent:
        return
    token = new_token()
    db.add(PasswordReset(token_hash=token_hash(token), client_id=client.id, expires_at=utcnow() + RESET_TTL))
    db.commit()
    link = f"{get_settings().site_url.rstrip('/')}/clientlogin/reset/?token={token}"
    tasks.add_task(mailer.send_reset_link, client.email, client.name, link)


@router.post("/reset", status_code=204)
def reset(body: ResetIn, db: DB) -> None:
    row = db.get(PasswordReset, token_hash(body.token))
    client = db.get(Client, row.client_id) if row else None
    if not row or row.used or row.expires_at <= utcnow() or not client or not client.is_active:
        raise HTTPException(400, "This link has expired. Request a new one.")
    client.password_hash = hash_password(body.password)
    row.used = True
    # Log out every browser: whoever knew the old password loses access.
    db.execute(delete(LoginSession).where(LoginSession.client_id == client.id))
    db.commit()
