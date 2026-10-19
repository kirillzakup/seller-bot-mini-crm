"""FastAPI application entrypoint.

Boots the database, seeds defaults, runs the aiogram bot via long-polling in the
same process, and serves the admin panel + payment webhooks.

Run:  uvicorn app.main:app --reload
"""

from __future__ import annotations

import logging
import mimetypes
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.admin.app import router as admin_router
from app.appapi.routes import router as appapi_router
from app.bot.runner import reload_bot, shutdown_bot
from app.channels.meta_router import router as meta_router
from app.config import BASE_DIR, MEDIA_DIR, get_settings
from app.db.base import init_db
from app.landing import router as landing_router
from app.leads import router as leads_router
from app.payments.webhooks import router as payments_router
from app.scheduler import start_scheduler, stop_scheduler
from app.services.seed import seed_defaults

settings = get_settings()
logger = logging.getLogger("sellerbot")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    await init_db()
    await seed_defaults()

    # Starts polling if a token is configured (DB or env); otherwise logs and waits.
    await reload_bot()

    # Background jobs (expiry, abandoned-cart nudges, payment reconcile).
    # Skipped under tests so interval jobs don't fire during the suite.
    scheduler_on = not os.environ.get("SELLERBOT_TESTING")
    if scheduler_on:
        start_scheduler()

    try:
        yield
    finally:
        if scheduler_on:
            stop_scheduler()
        await shutdown_bot()


app = FastAPI(title="Seller-bot", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    max_age=60 * 60 * 24 * 7,
    same_site="lax",  # blocks cross-site POST from carrying the cookie (CSRF defence)
    https_only=settings.base_url.startswith("https"),  # Secure flag in production
)

# The company website (a different origin, SITE_ORIGINS in .env) posts its form to
# /leads via browser fetch — allow only the site's own origins to read the response.
# No credentials/cookies are involved, so the cookie-protected admin is unaffected.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.site_origins.split(",") if o.strip()],
    allow_methods=["POST", "OPTIONS"],
    allow_headers=["*"],
    max_age=3600,
)

app.mount(
    "/static",
    StaticFiles(directory=str(BASE_DIR / "app" / "admin" / "static")),
    name="static",
)

# WhatsApp/Instagram infer a media file's format from the Content-Type our server returns
# when they fetch a /media link — NOT from the bytes. The container's default mimetypes
# registry doesn't know these audio extensions, so StaticFiles would serve them as
# application/octet-stream, which WhatsApp rejects (error 131053 "Unsupported Audio mime
# type"). Register the correct types so voice notes (.ogg) and other audio deliver.
for _ext, _mime in {
    ".ogg": "audio/ogg", ".oga": "audio/ogg", ".opus": "audio/ogg",
    ".m4a": "audio/mp4", ".aac": "audio/aac", ".weba": "audio/webm",
    ".mp3": "audio/mpeg", ".amr": "audio/amr",
}.items():
    mimetypes.add_type(_mime, _ext)

# Uploaded images/memes, served publicly so Telegram/WhatsApp/Instagram can fetch
# them by link when the bot sends one. Created on import so the mount never fails.
MEDIA_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/media", StaticFiles(directory=str(MEDIA_DIR)), name="media")

app.include_router(admin_router)
app.include_router(meta_router)  # public: /webhooks/meta (WhatsApp + Instagram)
app.include_router(payments_router)  # public: /webhooks/pay/{provider} + /pay/epay/{code}
app.include_router(landing_router)  # public: /l/{creative-code}
app.include_router(leads_router)  # public: POST /leads (web form on the site → operators)
app.include_router(appapi_router)  # JSON API for the native «Диалоги» app (bearer token)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/sw.js")
async def service_worker() -> FileResponse:
    """Serve the service worker from root so its scope covers /admin (PWA + notifications)."""
    return FileResponse(
        BASE_DIR / "app" / "admin" / "static" / "sw.js", media_type="application/javascript"
    )


@app.get("/")
async def root() -> RedirectResponse:
    return RedirectResponse(url="/admin")
