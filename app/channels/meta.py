"""Meta (WhatsApp Cloud API + Instagram Messaging API) adapter.

Both channels share Graph API: inbound via one webhook (distinguished by the
top-level ``object``), outbound via ``POST {graph}/{id}/messages``, integrity via
``X-Hub-Signature-256`` (HMAC-SHA256 of the raw body with the app secret).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
from collections import deque
from urllib.parse import urlparse

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.base import IncomingComment, IncomingMessage, Messenger
from app.config import MEDIA_DIR
from app.db.models.enums import Channel
from app.services.settings_service import get_setting

logger = logging.getLogger(__name__)

GRAPH = "https://graph.facebook.com"
# The new "Instagram API with Instagram login" serves messaging from its OWN host
# (not graph.facebook.com) and authenticates with an Instagram User token (IGAA…).
GRAPH_IG = "https://graph.instagram.com"
DEFAULT_GRAPH_VERSION = "v21.0"

# Instagram echoes every outbound message back to us as a webhook (is_echo=True).
# We remember (recipient, text) for everything WE send, so an incoming echo that
# matches is recognised as our own and skipped — while an UNMATCHED echo means the
# owner replied straight from the Instagram app, which we save to the client's
# history so the bot keeps context when it resumes.
_recent_sent: deque[tuple[str, str]] = deque(maxlen=200)


def note_sent(external_id: str, text: str) -> None:
    """Record an outbound message so its echo webhook can be recognised as ours."""
    if text:
        _recent_sent.append((str(external_id), text.strip()))


def was_bot_sent(external_id: str, text: str) -> bool:
    """True if (external_id, text) matches a message the bot recently sent."""
    return (str(external_id), (text or "").strip()) in _recent_sent


def _sent_message_id(resp) -> str | None:  # noqa: ANN001
    """Pull the outbound message id from a send response (IG: message_id, WA: messages[0].id)."""
    try:
        data = resp.json()
    except ValueError:
        return None
    mid = data.get("message_id")  # Instagram
    if mid:
        return str(mid)
    msgs = data.get("messages")  # WhatsApp
    if isinstance(msgs, list) and msgs:
        return str(msgs[0].get("id") or "") or None
    return None


def _read_local_media(url: str) -> bytes | None:
    """Read one of our own /media files by its public URL, to upload it to a channel
    instead of sending a link. Returns None if it isn't a local media file."""
    try:
        name = os.path.basename(urlparse(url or "").path)
        path = MEDIA_DIR / name
        if name and path.is_file():
            return path.read_bytes()
    except OSError:
        pass
    return None


async def get_meta_config(session: AsyncSession) -> dict:
    return await get_setting(session, "meta") or {}


async def get_reengage_template(session: AsyncSession) -> dict | None:
    """The approved WhatsApp template used to re-open a closed 24-hour window, or None
    if not configured. Shape: ``{"name", "language", "text"}`` — ``text`` is our own
    copy of the body, shown in the thread since Meta holds the real wording."""
    cfg = await get_meta_config(session)
    tmpl = (cfg.get("whatsapp") or {}).get("reengage_template") or {}
    name = (tmpl.get("name") or "").strip()
    if not name:
        return None
    return {
        "name": name,
        "language": (tmpl.get("language") or "ru").strip() or "ru",
        "text": (tmpl.get("text") or "").strip(),
    }


def webhook_secret(cfg: dict, obj: str | None) -> str:
    """Pick the app secret to verify a webhook signature against.

    Instagram webhooks are signed with the *Instagram* App Secret; WhatsApp with
    the *Meta* App Secret. Falls back to the Meta secret when the IG one isn't set.
    """
    if obj == "instagram":
        ig = cfg.get("instagram") or {}
        return ig.get("app_secret") or cfg.get("app_secret", "")
    return cfg.get("app_secret", "")


def ig_signature_optional(cfg: dict) -> bool:
    """True when Instagram webhook signatures can't be verified yet.

    Instagram signs webhooks with the *Instagram* App Secret (separate from the
    Meta App Secret, gated behind a password in the dashboard). Until it is set,
    the router accepts Instagram events WITHOUT HMAC verification — WhatsApp stays
    strict. Setting ``meta.instagram.app_secret`` re-enables verification here
    automatically.
    """
    ig = cfg.get("instagram") or {}
    return not (ig.get("app_secret") or "").strip()


def wa_signature_optional(cfg: dict) -> bool:
    """True when WhatsApp webhook signatures should be accepted without HMAC check.

    The Meta App Secret is gated behind a password re-prompt in the dashboard and
    can be awkward to retrieve. When ``meta.whatsapp.skip_signature`` is set, the
    router accepts WhatsApp events WITHOUT verifying ``X-Hub-Signature-256`` — a
    temporary measure until the correct app secret is configured. Clear the flag
    (or set ``meta.app_secret`` to the app's real secret) to re-enable the check.
    """
    wa = cfg.get("whatsapp") or {}
    return bool(wa.get("skip_signature"))


def verify_signature(app_secret: str, body: bytes, signature_header: str | None) -> bool:
    """Verify Meta's X-Hub-Signature-256 header. Fails closed if unconfigured."""
    if not app_secret or not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def parse_receipts(payload: dict) -> list[dict]:
    """Delivery/read receipts from a webhook: WhatsApp per-message statuses and
    Instagram read events. Returns dicts: ``{"mid", "status"}`` (WA) or
    ``{"read_by": <client igsid>}`` (IG marks all that client's outbound read)."""
    obj = payload.get("object")
    out: list[dict] = []
    if obj == "whatsapp_business_account":
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                for st in ((change.get("value") or {}).get("statuses") or []):
                    mid, status = st.get("id"), st.get("status")
                    if mid and status in ("sent", "delivered", "read", "failed"):
                        if status == "failed":
                            logger.warning(
                                "WA delivery failed mid=%s: %s", mid, st.get("errors") or []
                            )
                        out.append({"mid": str(mid), "status": status})
    elif obj == "instagram":
        for entry in payload.get("entry", []):
            for ev in entry.get("messaging", []):
                if ev.get("read"):
                    sender = (ev.get("sender") or {}).get("id")
                    if sender:
                        out.append({"read_by": str(sender)})
    return out


def parse_webhook(payload: dict) -> list[IncomingMessage]:
    """Normalize a Meta webhook payload into IncomingMessage objects."""
    obj = payload.get("object")
    out: list[IncomingMessage] = []

    if obj == "whatsapp_business_account":
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                names = {
                    c.get("wa_id"): (c.get("profile") or {}).get("name")
                    for c in value.get("contacts", [])
                }
                for m in value.get("messages", []):
                    sender = m.get("from")
                    text = (m.get("text") or {}).get("body")
                    attachments = []
                    if m.get("type") == "image" and m.get("image"):
                        attachments.append({"kind": "image", "file_id": m["image"].get("id")})
                    if m.get("type") == "audio" and m.get("audio"):
                        # WhatsApp voice notes also arrive as type "audio".
                        attachments.append({"kind": "audio", "file_id": m["audio"].get("id")})
                    if m.get("type") == "document" and m.get("document"):
                        # Kaspi receipts are often sent as a PDF document — don't drop them.
                        doc = m["document"]
                        attachments.append({
                            "kind": "document",
                            "file_id": doc.get("id"),
                            "mime": doc.get("mime_type"),
                            "filename": doc.get("filename"),
                        })
                    if not text and (m.get("caption") or {}):
                        # image/document captions live alongside the media object.
                        text = (m.get(m.get("type")) or {}).get("caption")
                    out.append(
                        IncomingMessage(
                            channel=Channel.WHATSAPP,
                            external_id=str(sender),
                            text=text,
                            name=names.get(sender),
                            attachments=attachments,
                            external_message_id=m.get("id"),
                            raw=m,
                        )
                    )

    elif obj == "instagram":
        for entry in payload.get("entry", []):
            for ev in entry.get("messaging", []):
                sender = (ev.get("sender") or {}).get("id")
                recipient = (ev.get("recipient") or {}).get("id")

                # Reaction event (emoji reaction to a message/story) — no message body.
                reaction = ev.get("reaction")
                if reaction:
                    if sender and reaction.get("action") == "react":
                        out.append(
                            IncomingMessage(
                                channel=Channel.INSTAGRAM,
                                external_id=str(sender),
                                reaction=reaction.get("emoji") or "❤️",
                                raw=ev,
                            )
                        )
                    continue

                message = ev.get("message")
                if not message:
                    continue
                is_echo = bool(message.get("is_echo"))
                # For an echo the business is the sender and the CLIENT is the recipient;
                # key it by the client so the message lands in the right history.
                ext = recipient if is_echo else sender
                if not ext:
                    continue

                text = message.get("text")
                story_reply = bool((message.get("reply_to") or {}).get("story"))
                attachments = []
                for att in message.get("attachments") or []:
                    atype = att.get("type")
                    if atype == "image":
                        attachments.append(
                            {"kind": "image", "url": (att.get("payload") or {}).get("url")}
                        )
                    elif atype == "audio":
                        attachments.append(
                            {"kind": "audio", "url": (att.get("payload") or {}).get("url")}
                        )
                    elif atype == "story_mention":
                        story_reply = True  # a story mention → treat like a story reply

                if text or attachments or is_echo or story_reply:
                    out.append(
                        IncomingMessage(
                            channel=Channel.INSTAGRAM,
                            external_id=str(ext),
                            text=text,
                            attachments=attachments,
                            is_echo=is_echo,
                            story_reply=story_reply,
                            external_message_id=message.get("mid"),
                            raw=ev,
                        )
                    )

    return out


def parse_comments(payload: dict) -> list[IncomingComment]:
    """Instagram comment webhooks: object=instagram, entry[].changes[] field=comments."""
    if payload.get("object") != "instagram":
        return []
    out: list[IncomingComment] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            if change.get("field") != "comments":
                continue
            v = change.get("value") or {}
            cid = v.get("id")
            if not cid:
                continue
            frm = v.get("from") or {}
            out.append(
                IncomingComment(
                    channel=Channel.INSTAGRAM,
                    comment_id=str(cid),
                    text=v.get("text") or "",
                    from_id=str(frm.get("id") or ""),
                    from_username=frm.get("username"),
                    media_id=(v.get("media") or {}).get("id"),
                    parent_id=v.get("parent_id"),
                    raw=v,
                )
            )
    return out


class MetaMessenger(Messenger):
    def __init__(
        self,
        channel: Channel,
        *,
        token: str,
        sender_id: str,
        graph_version: str,
        human_agent: bool = False,
    ) -> None:
        self.channel = channel
        self._token = token
        self._sender_id = sender_id  # WA phone_number_id OR IG user id
        self._ver = graph_version
        # Instagram only: tag outbound as a human-agent reply to extend the messaging
        # window from 24h to 7 days (needs the Human Agent feature approved for the app).
        self._human_agent = human_agent
        # Instagram (new API) → graph.instagram.com, addressed as `me` (the token's
        # own account). WhatsApp → graph.facebook.com, addressed by phone_number_id.
        if channel == Channel.INSTAGRAM:
            self._base = GRAPH_IG
            self._msg_owner = "me"
        else:
            self._base = GRAPH
            self._msg_owner = sender_id

    def _messages_url(self) -> str:
        """The `…/messages` send endpoint for this channel (host + owner differ)."""
        return f"{self._base}/{self._ver}/{self._msg_owner}/messages"

    async def send_text(self, external_id: str, text: str) -> str | None:
        if not self._token or not self._sender_id:
            logger.warning("Meta channel %s is not configured — message dropped", self.channel)
            return None
        url = self._messages_url()
        if self.channel == Channel.WHATSAPP:
            data = {
                "messaging_product": "whatsapp",
                "to": external_id,
                "type": "text",
                "text": {"body": text},
            }
        else:  # Instagram
            data = {"recipient": {"id": external_id}, "message": {"text": text}}
            if self._human_agent:
                data["messaging_type"] = "MESSAGE_TAG"
                data["tag"] = "HUMAN_AGENT"
        headers = {"Authorization": f"Bearer {self._token}"}
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
                resp = await client.post(url, json=data, headers=headers)
            if resp.status_code >= 400:
                logger.error("Meta send failed %s: %s", resp.status_code, resp.text)
                return None
            if self.channel == Channel.INSTAGRAM:
                note_sent(external_id, text)  # so our own echo webhook is recognised
            mid = _sent_message_id(resp)
            if not mid:  # 200 with no message id = "accepted" but not actually delivered
                logger.warning(
                    "Meta send: no message id (%s) → %s", resp.status_code, resp.text[:400]
                )
            return mid
        except httpx.HTTPError as e:
            logger.error("Meta send error: %s", e)
            return None

    async def send_reaction(self, external_id: str, message_mid: str, emoji: str) -> bool:
        """React to a message. WhatsApp supports message reactions natively (an empty
        emoji removes the reaction); the Instagram messaging API does not, so we no-op
        there and the reaction stays panel-only."""
        if self.channel != Channel.WHATSAPP or not self._token or not message_mid:
            return False
        data = {
            "messaging_product": "whatsapp",
            "to": external_id,
            "type": "reaction",
            "reaction": {"message_id": message_mid, "emoji": emoji or ""},
        }
        headers = {"Authorization": f"Bearer {self._token}"}
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=8.0)) as client:
                resp = await client.post(self._messages_url(), json=data, headers=headers)
            if resp.status_code >= 400:
                logger.warning("WA reaction failed %s: %s", resp.status_code, resp.text[:150])
                return False
            return True
        except httpx.HTTPError as e:
            logger.debug("WA reaction error: %s", e)
            return False

    async def send_template(
        self,
        external_id: str,
        *,
        name: str,
        language: str = "ru",
        body_params: list[str] | None = None,
    ) -> str | None:
        """Send an approved WhatsApp template — the only way to reach a client after the
        24-hour customer-care window has closed. No-op on non-WhatsApp channels."""
        if self.channel != Channel.WHATSAPP:
            return None
        if not self._token or not self._sender_id:
            logger.warning("WhatsApp is not configured — template dropped")
            return None
        template: dict = {"name": name, "language": {"code": language}}
        if body_params:
            template["components"] = [
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": str(p)} for p in body_params],
                }
            ]
        data = {
            "messaging_product": "whatsapp",
            "to": external_id,
            "type": "template",
            "template": template,
        }
        headers = {"Authorization": f"Bearer {self._token}"}
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
                resp = await client.post(self._messages_url(), json=data, headers=headers)
            if resp.status_code >= 400:
                logger.error("WA template send failed %s: %s", resp.status_code, resp.text)
                return None
            return _sent_message_id(resp)
        except httpx.HTTPError as e:
            logger.error("WA template send error: %s", e)
            return None

    async def send_typing(self, external_id: str, *, message_id: str | None = None) -> None:
        """Show a typing indicator. Instagram uses ``sender_action=typing_on``;
        WhatsApp rides it on a read receipt for the inbound message (needs its id)."""
        if not self._token or not self._sender_id:
            return
        headers = {"Authorization": f"Bearer {self._token}"}
        if self.channel == Channel.INSTAGRAM:
            data: dict = {"recipient": {"id": external_id}, "sender_action": "typing_on"}
        elif message_id:
            data = {
                "messaging_product": "whatsapp",
                "status": "read",
                "message_id": message_id,
                "typing_indicator": {"type": "text"},
            }
        else:
            return  # WhatsApp has no id-less typing indicator
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
                await client.post(self._messages_url(), json=data, headers=headers)
        except httpx.HTTPError as e:
            logger.debug("Meta typing indicator failed: %s", e)

    async def fetch_profile(self, external_id: str) -> dict | None:
        """Resolve an Instagram IGSID to ``{name, username}`` via the User Profile
        API (works for people who've messaged the account). WhatsApp has no handle."""
        if self.channel != Channel.INSTAGRAM or not self._token:
            return None
        url = f"{self._base}/{self._ver}/{external_id}"
        params = {"fields": "name,username", "access_token": self._token}
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=8.0)) as client:
                resp = await client.get(url, params=params)
            if resp.status_code >= 400:
                logger.debug("IG profile fetch %s: %s", resp.status_code, resp.text[:150])
                return None
            data = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            logger.debug("IG profile fetch error: %s", e)
            return None
        return {"name": data.get("name"), "username": data.get("username")}

    async def fetch_media_caption(self, media_id: str) -> str | None:
        """Fetch the caption of the Instagram post/reel a comment was left under, so
        the bot replies in context (e.g. knows Галина is our *student*, not a teacher)."""
        if self.channel != Channel.INSTAGRAM or not self._token or not media_id:
            return None
        url = f"{self._base}/{self._ver}/{media_id}"
        params = {"fields": "caption", "access_token": self._token}
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=8.0)) as client:
                resp = await client.get(url, params=params)
            if resp.status_code >= 400:
                logger.debug("IG media caption %s: %s", resp.status_code, resp.text[:150])
                return None
            caption = (resp.json().get("caption") or "").strip()
        except (httpx.HTTPError, ValueError) as e:
            logger.debug("IG media caption error: %s", e)
            return None
        return caption or None

    async def fetch_comments(self, media_id: str, *, limit: int = 40) -> list[dict]:
        """Fetch recent comments on an Instagram post/reel (with replies), oldest-first,
        so the bot reads the full thread before answering one comment."""
        if self.channel != Channel.INSTAGRAM or not self._token or not media_id:
            return []
        url = f"{self._base}/{self._ver}/{media_id}/comments"
        params = {
            "fields": "text,username,timestamp,replies{text,username,timestamp}",
            "limit": limit,
            "access_token": self._token,
        }
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=8.0)) as client:
                resp = await client.get(url, params=params)
            if resp.status_code >= 400:
                logger.debug("IG comments %s: %s", resp.status_code, resp.text[:150])
                return []
            data = (resp.json().get("data") or [])
        except (httpx.HTTPError, ValueError) as e:
            logger.debug("IG comments error: %s", e)
            return []
        out: list[dict] = []
        for c in data:  # API returns newest-first → reverse to read the thread in order
            for node in [c, *((c.get("replies") or {}).get("data") or [])]:
                text = (node.get("text") or "").strip()
                if text:
                    out.append({"from": (node.get("username") or "").strip(), "text": text})
        out.reverse()
        return out

    async def fetch_conversations(
        self, *, max_threads: int = 30, per_thread: int = 40
    ) -> list[dict]:
        """Pull recent Instagram DM threads with their messages, oldest-first, for
        style learning. Returns ``[{messages: [{from, text, ts}]}]`` (empty if off)."""
        if self.channel != Channel.INSTAGRAM or not self._token:
            return []
        base = f"{self._base}/{self._ver}"
        fields = f"messages.limit({per_thread})" "{message,from,created_time}"
        out: list[dict] = []
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=8.0)) as client:
                lr = await client.get(
                    f"{base}/me/conversations",
                    params={"fields": "id", "limit": max_threads, "access_token": self._token},
                )
                if lr.status_code >= 400:
                    logger.warning("IG conversations %s: %s", lr.status_code, lr.text[:150])
                    return []
                for th in (lr.json().get("data") or [])[:max_threads]:
                    cid = th.get("id")
                    if not cid:
                        continue
                    mr = await client.get(
                        f"{base}/{cid}",
                        params={"fields": fields, "access_token": self._token},
                    )
                    if mr.status_code >= 400:
                        continue
                    raw = ((mr.json().get("messages") or {}).get("data")) or []
                    items = [
                        {
                            "from": (m.get("from") or {}).get("username") or "",
                            "text": (m.get("message") or "").strip(),
                            "ts": m.get("created_time") or "",
                        }
                        for m in reversed(raw)  # API returns newest-first → want oldest-first
                        if (m.get("message") or "").strip()
                    ]
                    if items:
                        out.append({"messages": items})
        except (httpx.HTTPError, ValueError) as e:
            logger.warning("IG conversations error: %s", e)
        return out

    async def _ig_post(self, url: str, data: dict) -> bool:
        """POST helper for Instagram comment ops (reply/hide/private reply)."""
        if self.channel != Channel.INSTAGRAM or not self._token:
            return False
        headers = {"Authorization": f"Bearer {self._token}"}
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=8.0)) as client:
                resp = await client.post(url, json=data, headers=headers)
            if resp.status_code >= 400:
                logger.error("IG comment op failed %s: %s", resp.status_code, resp.text[:200])
                return False
            return True
        except httpx.HTTPError as e:
            logger.error("IG comment op error: %s", e)
            return False

    async def reply_comment(self, comment_id: str, text: str) -> bool:
        """Post a public reply under an Instagram comment."""
        url = f"{self._base}/{self._ver}/{comment_id}/replies"
        return await self._ig_post(url, {"message": text})

    async def hide_comment(self, comment_id: str, hide: bool = True) -> bool:
        """Hide/unhide a comment (the API has no 'like' — hide is the moderation op)."""
        url = f"{self._base}/{self._ver}/{comment_id}"
        return await self._ig_post(url, {"hide": hide})

    async def private_reply(self, comment_id: str, text: str) -> bool:
        """DM the comment's author in response — moves them into Direct."""
        return await self._ig_post(
            self._messages_url(),
            {"recipient": {"comment_id": comment_id}, "message": {"text": text}},
        )

    async def private_reply_image(self, comment_id: str, url: str) -> bool:
        """DM an image to the comment's author (Instagram attachment by public URL)."""
        return await self._ig_post(
            self._messages_url(),
            {
                "recipient": {"comment_id": comment_id},
                "message": {"attachment": {"type": "image", "payload": {"url": url}}},
            },
        )

    async def send_video_note(self, external_id: str, url: str, caption: str = "") -> None:
        """Send a round video note: WhatsApp has no round notes, so send as a regular video.
        Instagram also has no native round notes. Caption is sent separately as text."""
        await self.send_media(
            external_id,
            url=url,
            mime="video/mp4",
            kind="video",
            caption=caption or "",
        )

    async def send_image(self, external_id: str, url: str, caption: str = "") -> None:
        """Send an image by public link (WhatsApp) or attachment URL (Instagram).

        Falls back to sending the link as text on any failure or if unconfigured.
        """
        if not self._token or not self._sender_id:
            await self.send_text(external_id, f"{caption}\n{url}".strip() if caption else url)
            return
        endpoint = self._messages_url()
        headers = {"Authorization": f"Bearer {self._token}"}
        if self.channel == Channel.WHATSAPP:
            image: dict = {"link": url}
            if caption:
                image["caption"] = caption
            data = {
                "messaging_product": "whatsapp",
                "to": external_id,
                "type": "image",
                "image": image,
            }
        else:  # Instagram — attachment by URL (caption sent separately below)
            data = {
                "recipient": {"id": external_id},
                "message": {"attachment": {"type": "image", "payload": {"url": url}}},
            }
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
                resp = await client.post(endpoint, json=data, headers=headers)
                if resp.status_code >= 400:
                    logger.error("Meta image send failed %s: %s", resp.status_code, resp.text)
                    await self.send_text(
                        external_id, f"{caption}\n{url}".strip() if caption else url
                    )
                    return
            if self.channel == Channel.INSTAGRAM and caption:
                await self.send_text(external_id, caption)
        except httpx.HTTPError as e:
            logger.error("Meta image send error: %s", e)
            await self.send_text(external_id, f"{caption}\n{url}".strip() if caption else url)

    async def send_media(
        self,
        external_id: str,
        *,
        url: str,
        mime: str,
        kind: str,
        filename: str = "",
        caption: str = "",
    ) -> str | None:
        """Send a photo/video/voice/document by public URL. WhatsApp uses a media
        ``link``; Instagram uses an attachment payload (image/video/audio only — other
        files fall back to a link). Caption is inlined where allowed, else sent after."""
        link_fallback = f"{caption}\n{url}".strip() if caption else url
        if not self._token or not self._sender_id:
            return await self.send_text(external_id, link_fallback)
        # WhatsApp voice notes must be UPLOADED, not sent by link: by-link audio delivers but
        # is unplayable on the client ("Данное аудио более недоступно"). Upload our local
        # file and send it by media id — the same reliable path the bot's voice uses.
        if self.channel == Channel.WHATSAPP and kind == "voice":
            data = _read_local_media(url)
            if data is not None:
                mid = await self._wa_upload_audio(
                    external_id, data, mime or "audio/ogg", filename or "voice.ogg"
                )
                if mid:
                    if caption:
                        await self.send_text(external_id, caption)
                    return mid
                # upload failed → fall through to the link path below (then text fallback)
        endpoint = self._messages_url()
        headers = {"Authorization": f"Bearer {self._token}"}
        inline_caption = False
        if self.channel == Channel.WHATSAPP:
            wa_type = {"photo": "image", "video": "video", "voice": "audio"}.get(kind, "document")
            media: dict = {"link": url}
            if wa_type in ("image", "video", "document") and caption:
                media["caption"] = caption
                inline_caption = True
            if wa_type == "document" and filename:
                media["filename"] = filename
            data = {
                "messaging_product": "whatsapp",
                "to": external_id,
                "type": wa_type,
                wa_type: media,
            }
        else:  # Instagram
            ig_type = {"photo": "image", "video": "video", "voice": "audio"}.get(kind)
            if ig_type is None:  # IG DMs can't carry arbitrary files → send the link
                return await self.send_text(external_id, link_fallback)
            data = {
                "recipient": {"id": external_id},
                "message": {"attachment": {"type": ig_type, "payload": {"url": url}}},
            }
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
                resp = await client.post(endpoint, json=data, headers=headers)
            if resp.status_code >= 400:
                logger.error("Meta media send failed %s: %s", resp.status_code, resp.text)
                return await self.send_text(external_id, link_fallback)
            mid = _sent_message_id(resp)
            if not mid:  # 200 with no message id = "accepted" but not actually delivered
                logger.warning(
                    "Meta send: no message id (%s) → %s", resp.status_code, resp.text[:400]
                )
            if caption and not inline_caption:  # audio / IG carry no caption → send it as text
                await self.send_text(external_id, caption)
            return mid
        except httpx.HTTPError as e:
            logger.error("Meta media send error: %s", e)
            return await self.send_text(external_id, link_fallback)

    async def send_voice(
        self, external_id: str, audio: bytes, *, mime: str, filename: str
    ) -> bool:
        """Send a voice reply on WhatsApp (upload to /media → send type=audio).

        Returns True if delivered; False (→ caller falls back to text) on any
        failure or on Instagram (audio-out via the IG API is not supported here).
        """
        if self.channel != Channel.WHATSAPP or not self._token or not self._sender_id:
            return False
        return bool(await self._wa_upload_audio(external_id, audio, mime, filename))

    async def send_audio_by_url(self, external_id: str, url: str, *, mime: str) -> bool:
        """Instagram has no voice-note upload, but its DM API accepts an audio attachment
        by URL (aac/m4a/wav/mp4) — so the bot can also SPEAK on Instagram. Returns True only
        if IG accepted it; on any error returns False so the caller falls back to text
        (never leaks the raw URL to the client)."""
        if self.channel != Channel.INSTAGRAM or not self._token or not url:
            return False
        data = {
            "recipient": {"id": external_id},
            "message": {"attachment": {"type": "audio", "payload": {"url": url}}},
        }
        headers = {"Authorization": f"Bearer {self._token}"}
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
                resp = await client.post(self._messages_url(), json=data, headers=headers)
            if resp.status_code >= 400:
                logger.error("IG audio send failed %s: %s", resp.status_code, resp.text[:300])
                return False
            return _sent_message_id(resp) is not None
        except httpx.HTTPError as e:
            logger.error("IG audio send error: %s", e)
            return False

    async def _wa_upload_audio(
        self, external_id: str, audio: bytes, mime: str, filename: str
    ) -> str | None:
        """Upload audio bytes to WhatsApp and send them as a voice note (type=audio).
        Returns the sent message id, else None. Reliable path for BOTH the bot's replies and
        operator voice — by-link audio is accepted but often unplayable on the client."""
        headers = {"Authorization": f"Bearer {self._token}"}
        up_type = (mime or "audio/ogg").split(";")[0].strip() or "audio/ogg"  # WA wants bare mime
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
                up = await client.post(
                    f"{self._base}/{self._ver}/{self._sender_id}/media",
                    data={"messaging_product": "whatsapp", "type": up_type},
                    files={"file": (filename, audio, up_type)},
                    headers=headers,
                )
                if up.status_code >= 400:
                    logger.error("WA media upload failed %s: %s", up.status_code, up.text)
                    return None
                media_id = up.json().get("id")
                if not media_id:
                    return None
                resp = await client.post(
                    self._messages_url(),
                    json={
                        "messaging_product": "whatsapp",
                        "to": external_id,
                        "type": "audio",
                        # voice=True → deliver as a PTT voice note (the render path OGG/Opus is
                        # meant for). Without it WA treats OGG/Opus as a generic audio attachment
                        # and many clients show "audio no longer available" / can't play it.
                        "audio": {"id": media_id, "voice": True},
                    },
                    headers=headers,
                )
                if resp.status_code >= 400:
                    logger.error("WA voice send failed %s: %s", resp.status_code, resp.text)
                    return None
                return _sent_message_id(resp)
        except httpx.HTTPError as e:
            logger.error("WA voice send error: %s", e)
            return None

    async def download_media(self, ref: dict) -> bytes | None:
        """Fetch inbound media bytes (audio, receipt image, …).

        WhatsApp gives a media id → resolve to a (token-gated) URL, then download.
        Instagram gives a pre-signed CDN URL that is fetched directly.
        """
        if not self._token:
            return None
        headers = {"Authorization": f"Bearer {self._token}"}
        url = ref.get("url")
        try:
            timeout = httpx.Timeout(60.0, connect=10.0)
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                if not url:
                    media_id = ref.get("file_id")
                    if not media_id:
                        return None
                    meta = await client.get(f"{self._base}/{self._ver}/{media_id}", headers=headers)
                    if meta.status_code >= 400:
                        logger.error("Meta media lookup failed %s", meta.status_code)
                        return None
                    url = meta.json().get("url")
                    if not url:
                        return None
                    resp = await client.get(url, headers=headers)  # WA media needs the token
                else:
                    resp = await client.get(url)  # IG CDN URL is already signed
                if resp.status_code >= 400:
                    logger.error("Meta media download failed %s", resp.status_code)
                    return None
                return resp.content or None
        except (httpx.HTTPError, ValueError) as e:
            logger.error("Meta media download error: %s", e)
            return None

    async def download_audio(self, ref: dict) -> bytes | None:
        """Backward-compatible alias — inbound audio is just media bytes."""
        return await self.download_media(ref)


async def build_meta_messenger(session: AsyncSession, channel: Channel) -> MetaMessenger:
    cfg = await get_meta_config(session)
    ver = cfg.get("graph_version") or DEFAULT_GRAPH_VERSION
    if channel == Channel.WHATSAPP:
        wa = cfg.get("whatsapp") or {}
        return MetaMessenger(
            channel,
            token=wa.get("access_token", ""),
            sender_id=wa.get("phone_number_id", ""),
            graph_version=ver,
        )
    ig = cfg.get("instagram") or {}
    return MetaMessenger(
        channel,
        token=ig.get("access_token", ""),
        sender_id=ig.get("ig_id", ""),
        graph_version=ver,
        human_agent=bool(ig.get("human_agent")),
    )
