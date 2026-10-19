"""Application configuration loaded from environment / .env.

Infrastructure-level settings only (bot token, DB, secrets, admin creds).
Business config that changes often (persona prompt, LLM model, operator chat,
payment requisites) lives in the DB `settings` table and is edited in the admin
panel — env values here only seed sensible defaults.
"""

from __future__ import annotations

import logging
import secrets
from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

_WEAK_SECRETS = {"", "change-me-please"}
_WEAK_PASSWORDS = {"", "admin"}

# Project layout anchors
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
UPLOADS_DIR = DATA_DIR / "uploads"
# Uploaded images/memes the bot can send in chats — served publicly at /media/…
# (lives under the data volume so it survives container rebuilds).
MEDIA_DIR = UPLOADS_DIR / "media"


class Settings(BaseSettings):
    """Typed settings, read from environment variables / .env file."""

    model_config = SettingsConfigDict(
        # Bootstrap secrets in .env; admin-managed values in runtime/.env (the
        # latter is written from the panel and overrides on the next start).
        env_file=(BASE_DIR / ".env", BASE_DIR / "runtime" / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Telegram bot ---
    bot_token: str = Field(default="", description="Telegram bot token from @BotFather")

    # --- Web / server ---
    base_url: str = Field(default="http://localhost:8000", description="Public base URL (webhooks)")
    # Сайт компании, чья форма шлёт заявки в POST /leads (через запятую). Пусто — CORS закрыт.
    site_origins: str = Field(default="", description="Website origins allowed to POST /leads")
    host: str = "0.0.0.0"
    port: int = 8000

    # --- Database ---
    database_url: str = "sqlite+aiosqlite:///./data/sellerbot.db"

    # --- Admin panel auth ---
    admin_username: str = "admin"
    admin_password: str = "admin"
    secret_key: str = "change-me-please"

    # --- LLM defaults (overridable via admin DB settings) ---
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-5.5"
    llm_temperature: float = 0.4

    # --- Voice / ElevenLabs defaults (overridable via admin DB settings) ---
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = ""

    # --- Uploads ---
    # Max size (MB) for a file an operator sends to a client from «Диалоги» and for
    # documents uploaded to the library. Keep the proxy's client_max_body_size ≥ this.
    operator_max_upload_mb: int = Field(default=100, description="Max operator upload size, MB")

    # --- Misc ---
    timezone: str = "Asia/Almaty"
    currency: str = "KZT"
    log_level: str = "INFO"

    @model_validator(mode="after")
    def _harden_secrets(self) -> Settings:
        """Fail safe against an insecure deploy: never run with the guessable default
        session key (generate an ephemeral random one), and loudly warn on a default
        admin password. In production both are set in .env, so this is a no-op there."""
        if self.secret_key in _WEAK_SECRETS:
            self.secret_key = secrets.token_urlsafe(48)
            logger.warning(
                "SECRET_KEY not set — using a random ephemeral key (sessions reset on "
                "restart). Set SECRET_KEY in .env for stable, secure sessions."
            )
        if self.admin_password in _WEAK_PASSWORDS:
            logger.warning(
                "ADMIN_PASSWORD is the default 'admin' — CHANGE IT in .env before "
                "exposing the admin panel."
            )
        return self

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache
def get_settings() -> Settings:
    """Cached singleton accessor for settings."""
    return Settings()
