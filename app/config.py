"""All settings come from environment variables (or .env). Defaults are for local dev."""
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _csv(value: str) -> list[str]:
    return [v.strip().rstrip("/") for v in value.split(",") if v.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./dev.db"
    # The website. Reset links point here, and only these origins may call the API with cookies.
    site_url: str = "http://localhost:4321"
    allowed_origins: str = "http://localhost:4321"  # comma-separated
    # Hostnames the API answers on (comma-separated). Empty = any. In production it's only the
    # custom domain, so the host's default address (e.g. *.onrender.com) returns 404. /health always answers.
    allowed_hosts: str = ""
    # This API's own address, for URLs it hands out (map images). Empty = https:// + the first allowed host.
    api_url: str = ""
    # BSA analyses running at once (each big statement needs ~100 MB; Render free has 512 MB), how many more may wait
    # for a slot, and for how long; beyond that a call gets a free 429 busy. Raise them on a bigger server.
    bsa_max_parallel: int = 2
    bsa_queue_max: int = 8
    bsa_queue_wait: float = 60.0
    cookie_secure: bool = False  # True in production (HTTPS only)
    session_days: int = 14

    # Password-reset mail. Brevo's HTTPS API is used when the key is set (Render's free tier blocks
    # SMTP ports); otherwise SMTP when smtp_host is set; otherwise the link is only logged (dev).
    mail_from: str = ""
    brevo_api_key: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""

    @field_validator("database_url")
    @classmethod
    def _psycopg_driver(cls, url: str) -> str:
        """Hosted Postgres (Neon, Render) hands out postgres:// URLs; SQLAlchemy needs the driver name."""
        for prefix in ("postgres://", "postgresql://"):
            if url.startswith(prefix):
                return "postgresql+psycopg://" + url[len(prefix):]
        return url

    @property
    def origins(self) -> list[str]:
        return _csv(self.allowed_origins)

    @property
    def hosts(self) -> list[str]:
        return [h.lower() for h in _csv(self.allowed_hosts)]

    @property
    def api_base(self) -> str:
        if self.api_url:
            return self.api_url.rstrip("/")
        return f"https://{self.hosts[0]}" if self.hosts else "http://localhost:8000"


@lru_cache
def get_settings() -> Settings:
    return Settings()
