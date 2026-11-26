from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import account, admin, api, auth, locate, models  # noqa: F401  (models registers the tables)
from .config import get_settings
from .db import Base, engine


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(engine)
    yield


settings = get_settings()
app = FastAPI(title="Client API", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def check_origin(request: Request, call_next):
    """CSRF guard: a write that carries the session cookie must come from the website."""
    if request.method not in ("GET", "HEAD", "OPTIONS") and not request.url.path.startswith("/v1/"):
        origin = request.headers.get("origin")
        if (origin and origin.rstrip("/") not in settings.origins) or (not origin and auth.COOKIE in request.cookies):
            return JSONResponse({"detail": "Origin not allowed."}, status_code=403)
    return await call_next(request)


@app.middleware("http")
async def check_host(request: Request, call_next):
    """Answer only on our own domain. /health stays open so the host's health checks pass."""
    host = request.headers.get("host", "").split(":")[0].lower()
    if settings.hosts and request.url.path != "/health" and host not in settings.hosts:
        return JSONResponse({"detail": "Not found."}, status_code=404)
    return await call_next(request)


# Added last so it runs first: preflight and CORS headers wrap everything, including 403s above.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.origins,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)

class OpenV1Cors:
    """/v1 is called from clients' own websites (Maps JavaScript API) with a key, never with cookies, so any
    origin may call it: answer preflights here and add a wildcard CORS header. Which sites may actually spend a
    key is checked per key (api.key_caller, Websites). Added after CORSMiddleware so it runs first."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/v1/"):
            return await self.app(scope, receive, send)
        if scope["method"] == "OPTIONS":
            await send({"type": "http.response.start", "status": 204, "headers": [
                (b"access-control-allow-origin", b"*"),
                (b"access-control-allow-methods", b"GET, POST"),
                (b"access-control-allow-headers", b"Authorization, Content-Type"),
                (b"access-control-max-age", b"600"),
            ]})
            return await send({"type": "http.response.body", "body": b""})

        async def send_with_cors(message):
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"access-control-allow-origin"]
                message = {**message, "headers": headers + [(b"access-control-allow-origin", b"*")]}
            await send(message)

        await self.app(scope, receive, send_with_cors)


app.add_middleware(OpenV1Cors)
app.add_exception_handler(api.ApiError, lambda _, e: api.error_response(e))


@app.exception_handler(RequestValidationError)
async def bad_json(request: Request, e: RequestValidationError):
    """Product calls get the /v1 error shape (e.g. a body that isn't JSON); everything else keeps FastAPI's."""
    if request.url.path.startswith(("/v1/", "/account/try/")):
        return api.error_response(api.ApiError(422, "invalid_request", "Send a JSON body."))
    return await request_validation_exception_handler(request, e)


app.include_router(auth.router)
app.include_router(account.router)
app.include_router(admin.router)
app.include_router(api.router)
app.include_router(locate.router)


@app.get("/health")
def health() -> dict:
    return {"ok": True}
