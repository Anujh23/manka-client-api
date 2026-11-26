"""Dashboard endpoints for a logged-in client (session cookie): balance, ledger, API keys, Try.

One API key per client: generating a new one revokes the old one.
"""
import re
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, File, Form, HTTPException, Response, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, update

from . import api, wallet
from .auth import CurrentClient
from .bsa.export import report_to_xlsx
from .db import DB, utcnow
from .models import ApiKey, LocationRequest, WalletEntry, Website
from .products import BY_NAME, PRICES, bsa, maps
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
    websites: list[str]  # origins allowed to use the key from a browser
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
        websites=list(db.scalars(select(Website.origin).where(Website.client_id == client.id).order_by(Website.origin))),
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


@router.post("/try/bsa")
def try_bsa(client: CurrentClient, db: DB, file: Annotated[UploadFile | None, File()] = None, password: Annotated[str | None, Form()] = None) -> dict:
    """Bank statement analysis from the dashboard (multipart, like /v1/bsa/analyze)."""
    return api.run_bsa(db, file, password, api.Caller(client=client, key=None))


@router.post("/bsa/excel")
def bsa_excel(client: CurrentClient, report: Annotated[dict, Body()]) -> Response:
    """The BSA report the dashboard already has, as the same Excel workbook BSA exports. Free: nothing is stored, the
    report is sent back by the page and turned into the file."""
    if not isinstance(report.get("meta"), dict):
        raise api.ApiError(422, "invalid_request", "Send a BSA report.")
    try:
        data = report_to_xlsx(report)
    except Exception:
        raise api.ApiError(422, "invalid_request", "Could not build the Excel file from this report.") from None
    name = re.sub(r"[^A-Za-z0-9_-]", "", str(report.get("id") or "report"))[:40] or "report"
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="bsa-report-{name}.xlsx"'})


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


ORIGIN = re.compile(r"^https?://[a-z0-9.-]+(:[0-9]{1,5})?$")
MAX_WEBSITES = 10


class WebsitesIn(BaseModel):
    websites: list[str] = Field(max_length=MAX_WEBSITES)


@router.post("/websites")
def set_websites(body: WebsitesIn, client: CurrentClient, db: DB) -> list[str]:
    """Replace the websites allowed to use the key from a browser (Maps JavaScript API)."""
    clean = []
    for raw in body.websites:
        w = raw.strip().rstrip("/").lower()
        if not w:
            continue
        if not ORIGIN.match(w):
            raise HTTPException(400, f"{raw.strip()} isn't a website address like https://yourcompany.com")
        if w not in clean:
            clean.append(w)
    db.execute(delete(Website).where(Website.client_id == client.id))
    db.add_all(Website(client_id=client.id, origin=w) for w in clean)
    db.commit()
    return sorted(clean)
