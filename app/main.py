from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import auth, models  # noqa: F401  (models registers the tables)
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
    if request.method not in ("GET", "HEAD", "OPTIONS"):
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

app.include_router(auth.router)


@app.get("/health")
def health() -> dict:
    return {"ok": True}
