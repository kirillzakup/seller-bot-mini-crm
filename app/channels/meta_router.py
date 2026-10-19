"""Public Meta webhook: GET verification + POST message intake.

Verification compares ``hub.verify_token`` to the configured one. Intake checks
the HMAC signature, then dispatches each message in the background so Meta gets a
fast 200 (the LLM reply can take a few seconds).
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Request, Response

from app.channels.dispatch import dispatch_comment, dispatch_incoming
from app.channels.meta import (
    get_meta_config,
    ig_signature_optional,
    parse_comments,
    parse_receipts,
    parse_webhook,
    verify_signature,
    wa_signature_optional,
    webhook_secret,
)
from app.db.base import AsyncSessionLocal, run_write_with_retry
from app.db.models.enums import Channel
from app.services import receipts as receipts_svc

router = APIRouter(tags=["meta-webhook"])
logger = logging.getLogger(__name__)

# Keep strong refs to background tasks so they aren't GC'd mid-flight.
_tasks: set[asyncio.Task] = set()


@router.get("/webhooks/meta")
async def verify(request: Request) -> Response:
    p = request.query_params
    async with AsyncSessionLocal() as session:
        cfg = await get_meta_config(session)
    token = p.get("hub.verify_token")
    if p.get("hub.mode") == "subscribe" and token and token == cfg.get("verify_token"):
        return Response(content=p.get("hub.challenge") or "", media_type="text/plain")
    return Response(status_code=403)


def _shadow_check(cfg: dict, obj: str, body: bytes, signature: str) -> None:
    """Считает подпись, ничего не отклоняя, и пишет вердикт в лог.

    Включать проверку подписи вслепую на живом канале нельзя: если настроен
    секрет не от того приложения, ВСЕ сообщения клиентов начнут отбрасываться
    с 403 и потеряются безвозвратно — Meta свой 200 уже получила и не повторит.
    Поэтому сначала смотрим по логам, совпала бы подпись на реальном трафике,
    и только потом снимаем послабление.
    """
    secret = webhook_secret(cfg, obj)
    if not secret:
        logger.warning("SHADOW %s: секрета нет — проверять нечем", obj)
        return
    if not signature:
        logger.warning("SHADOW %s: Meta не прислала заголовок подписи", obj)
        return
    ok = verify_signature(secret, body, signature)
    logger.warning(
        "SHADOW %s: подпись %s — проверку %s включать",
        obj,
        "СОВПАЛА" if ok else "НЕ совпала",
        "можно" if ok else "НЕЛЬЗЯ",
    )


async def _process(msg) -> None:
    # Retry on a transient DB lock: a dropped webhook here is a client message (incl. a
    # payment receipt) lost for good, since Meta already received our 200.
    await run_write_with_retry(
        lambda session: dispatch_incoming(session, msg),
        label=f"meta dispatch {msg.external_id}",
    )


async def _process_comment(comment) -> None:
    await run_write_with_retry(
        lambda session: dispatch_comment(session, comment),
        label=f"meta comment {comment.comment_id}",
    )


async def _apply_receipt(session, rec: dict) -> None:
    if "mid" in rec:  # WhatsApp per-message status
        await receipts_svc.apply_status(session, mid=rec["mid"], status=rec["status"])
    elif "read_by" in rec:  # Instagram read event → mark that client's outbound read
        await receipts_svc.mark_read(session, channel=Channel.INSTAGRAM, external_id=rec["read_by"])


async def _process_receipt(rec: dict) -> None:
    await run_write_with_retry(
        lambda session: _apply_receipt(session, rec),
        label=f"meta receipt {rec}",
    )


@router.post("/webhooks/meta")
async def receive(request: Request) -> Response:
    body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")

    # Parse first so we can route to the right app secret (WhatsApp = Meta secret,
    # Instagram = Instagram secret). Nothing is acted upon until the signature is
    # verified just below — json.loads executes nothing, it only reads the object.
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return Response(status_code=400)
    if not isinstance(payload, dict):
        return Response(status_code=400)

    async with AsyncSessionLocal() as session:
        cfg = await get_meta_config(session)

    obj = payload.get("object")
    if obj == "instagram" and ig_signature_optional(cfg):
        # Interim: Instagram App Secret not configured yet, so we can't verify the
        # HMAC. Accept IG events unverified (WhatsApp stays strict); this re-locks
        # automatically once meta.instagram.app_secret is set.
        logger.warning("Instagram webhook accepted WITHOUT signature check (IG app secret not set)")
        _shadow_check(cfg, obj, body, signature)
    elif obj == "whatsapp_business_account" and wa_signature_optional(cfg):
        # Interim: the Meta App Secret isn't configured, so accept WhatsApp events
        # unverified. Re-locks once meta.whatsapp.skip_signature is cleared and the
        # correct meta.app_secret is set.
        logger.warning("WhatsApp webhook accepted WITHOUT signature check (skip_signature set)")
        _shadow_check(cfg, obj, body, signature)
    elif not verify_signature(webhook_secret(cfg, obj), body, signature):
        logger.warning("Meta webhook: invalid signature")
        return Response(status_code=403)

    for msg in parse_webhook(payload):
        task = asyncio.create_task(_process(msg))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)

    for comment in parse_comments(payload):
        task = asyncio.create_task(_process_comment(comment))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)

    for rec in parse_receipts(payload):
        task = asyncio.create_task(_process_receipt(rec))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)

    return Response(content="EVENT_RECEIVED", media_type="text/plain")
