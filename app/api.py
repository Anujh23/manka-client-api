"""Public product API (/v1). Callers authenticate with `Authorization: Bearer mcc_…` (one key per client).

Every product route is built from app/products: validate the input (422, free), charge, run, and refund if the
product finds nothing (404) or fails (500). Charging before running means two calls can't spend the same rupee.
Errors have one shape: {"error": {"code", "message"}, "id"?}. The dashboard's Try uses execute() too (account.py).
"""
import logging
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import wallet
from .db import DB, utcnow
from .models import ApiCall, ApiKey, Client, LocationRequest
from .products import PRODUCTS, NotFound, Product, maps
from .security import token_hash

router = APIRouter(prefix="/v1")
log = logging.getLogger(__name__)



class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, call_id: int | None = None):
        self.status, self.code, self.message, self.call_id = status, code, message, call_id


def error_response(e: ApiError) -> JSONResponse:
    body: dict[str, Any] = {"error": {"code": e.code, "message": e.message}}
    if e.call_id:
        body["id"] = f"call_{e.call_id}"
    return JSONResponse(body, status_code=e.status)


def validation_message(e: ValidationError) -> str:
    parts = []
    for err in e.errors():
        where = ".".join(str(x) for x in err["loc"])
        msg = err["msg"].removeprefix("Value error, ")
        parts.append(f"{where}: {msg}" if where else msg)
    return "; ".join(parts)


@dataclass
class Caller:
    client: Client
    key: ApiKey | None  # None for the dashboard's Try


def key_caller(db: DB, authorization: Annotated[str | None, Header()] = None) -> Caller:
    scheme, _, raw = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not raw.strip():
        raise ApiError(401, "invalid_key", "Missing API key. Send 'Authorization: Bearer <key>'.")
    key = db.scalar(select(ApiKey).where(ApiKey.key_hash == token_hash(raw.strip()), ApiKey.revoked_at.is_(None)))
    client = db.get(Client, key.client_id) if key else None
    if not key or not client or not client.is_active:
        raise ApiError(401, "invalid_key", "Invalid API key.")
    return Caller(client=client, key=key)


KeyCaller = Annotated[Caller, Depends(key_caller)]


def _log_call(db: Session, caller: Caller, name: str, title: str, cost: int) -> ApiCall:
    """Charge the balance, then record the call. Commits."""
    if cost:
        try:
            wallet.debit(db, caller.client.id, cost, "usage", title)
        except wallet.InsufficientBalance:
            db.rollback()
            raise ApiError(402, "insufficient_balance", "Insufficient balance. Recharge to continue.") from None
    call = ApiCall(client_id=caller.client.id, key_id=caller.key.id if caller.key else None, product=name, cost_paise=cost)
    db.add(call)
    if caller.key:
        caller.key.last_used_at = utcnow()
    db.commit()
    return call


def execute(db: Session, product: Product, raw: Any, caller: Caller) -> dict:
    try:
        body = product.input.model_validate(raw if raw is not None else {})
    except ValidationError as e:
        raise ApiError(422, "invalid_request", validation_message(e)) from None

    call = _log_call(db, caller, product.name, product.title, product.price_paise)
    try:
        return {"id": f"call_{call.id}", **product.run(db, body, caller.client)}
    except Exception as e:
        db.rollback()
        call.ok = False
        if call.cost_paise:
            wallet.credit(db, caller.client.id, call.cost_paise, "refund", product.title)
        db.commit()
        if isinstance(e, NotFound):
            raise ApiError(404, "not_found", str(e), call.id) from None
        log.exception("product %s failed", product.name)
        raise ApiError(500, "server_error", "Something went wrong. You weren't charged.", call.id) from None


def endpoint(product: Product):
    """A /v1 route for a product: GET takes its input from the path, POST from the JSON body."""
    if product.method == "GET":
        def route(request: Request, caller: KeyCaller, db: DB):
            return execute(db, product, dict(request.path_params), caller)
    else:
        def route(caller: KeyCaller, db: DB, body: Annotated[Any, Body()] = None):
            return execute(db, product, body, caller)
    return route


for _p in PRODUCTS:
    router.add_api_route(_p.path, endpoint(_p), methods=[_p.method], name=_p.name)


@router.get("/ping")
def ping(caller: KeyCaller, db: DB) -> dict:
    """Free key check."""
    call = _log_call(db, caller, "ping", "Key check", 0)
    return {"id": f"call_{call.id}", "ok": True, "client": caller.client.name}


@router.get("/maps/link/{request_id}")
def maps_result(request_id: str, caller: KeyCaller, db: DB) -> dict:
    """Maps API result: status (pending | done | expired) and, once done, lat, lng, accuracy_m, located_at. Free."""
    r = db.scalar(select(LocationRequest).where(LocationRequest.public_id == request_id, LocationRequest.client_id == caller.client.id))
    if r is None:
        raise ApiError(404, "not_found", "Request not found.")
    return maps.view(r)
