"""Web Push (VAPID) — notify the operator's device even when the app is closed.

Works on Android and on iOS 16.4+ **only for a PWA added to the Home Screen**.
VAPID keys are generated once and persisted in settings (stable — regenerating
would invalidate existing subscriptions). Subscriptions (operator devices) are
stored in settings too. Sending is best-effort; dead endpoints are pruned.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from py_vapid import Vapid01 as Vapid
from pywebpush import WebPushException, webpush
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.settings_service import get_setting, set_setting

logger = logging.getLogger(__name__)

VAPID_SUB = "mailto:admin@example.com"  # VAPID contact (required by push services)


async def get_vapid(session: AsyncSession) -> dict:
    """Return {private_pem, public_key}, generating + persisting them once."""
    cfg = await get_setting(session, "push_vapid") or {}
    if cfg.get("private_pem") and cfg.get("public_key"):
        return cfg
    priv = ec.generate_private_key(ec.SECP256R1())
    private_pem = priv.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    point = priv.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    public_key = base64.urlsafe_b64encode(point).rstrip(b"=").decode()
    cfg = {"private_pem": private_pem, "public_key": public_key}
    await set_setting(session, "push_vapid", cfg)
    return cfg


async def add_subscription(session: AsyncSession, sub: dict) -> None:
    subs = await get_setting(session, "push_subscriptions") or []
    endpoint = sub.get("endpoint")
    if endpoint and endpoint not in {s.get("endpoint") for s in subs}:
        subs.append(sub)
        await set_setting(session, "push_subscriptions", subs)


def _send_one(sub: dict, payload: str, vapid: Vapid) -> int | None:
    """Blocking single send; returns an HTTP status to prune on (404/410) or None.

    ``vapid`` is a prepared Vapid instance — passing the PEM *string* here makes
    pywebpush mis-parse it (silent ValueError → no push ever), which is exactly the
    bug this replaced."""
    try:
        webpush(
            subscription_info=sub,
            data=payload,
            vapid_private_key=vapid,
            vapid_claims={"sub": VAPID_SUB},
        )
        return None
    except WebPushException as e:
        code = getattr(e.response, "status_code", None)
        if code in (404, 410):
            return code
        logger.warning("Web push failed: %s", e)
        return None
    except Exception:  # noqa: BLE001 — never let a bad key/payload fail silently again
        logger.exception("Web push send errored")
        return None


async def send_push(
    session: AsyncSession, title: str, body: str, url: str = "/admin/dialogs"
) -> int:
    """Push a notification to every stored operator device. Returns how many sent."""
    subs = await get_setting(session, "push_subscriptions") or []
    if not subs:
        return 0
    cfg = await get_vapid(session)
    try:
        vapid = Vapid.from_pem(cfg["private_pem"].encode())
    except Exception:  # noqa: BLE001
        logger.exception("VAPID key load failed — cannot send push")
        return 0
    payload = json.dumps({"title": title, "body": body, "url": url})
    sent, dead = 0, []
    for sub in subs:
        gone = await asyncio.to_thread(_send_one, sub, payload, vapid)
        if gone is not None:
            dead.append(sub.get("endpoint"))
        else:
            sent += 1
    if dead:
        remaining = [s for s in subs if s.get("endpoint") not in dead]
        await set_setting(session, "push_subscriptions", remaining)
    return sent
