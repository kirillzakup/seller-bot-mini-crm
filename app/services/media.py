"""Media library: images/memes the owner uploads for the bot to send in chats.

Files are stored under the data volume (``MEDIA_DIR``) and served publicly at
``/media/{filename}`` so Telegram / WhatsApp / Instagram can fetch them by link.
The AI picks one via the ``send_image`` tool, matched on ``title`` + ``tags``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import MEDIA_DIR, get_settings
from app.db.models import MediaAsset
from app.knowledge.rag import _tokens  # shared simple tokenizer (keyword overlap)

logger = logging.getLogger(__name__)

# content-type → stored extension (also the allow-list: only images)
ALLOWED_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}
MAX_BYTES = 8 * 1024 * 1024  # 8 MB


def ensure_media_dir() -> None:
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)


def public_url(filename: str) -> str:
    """Absolute URL a messaging platform can fetch (BASE_URL must be public)."""
    return f"{get_settings().base_url.rstrip('/')}/media/{filename}"


async def save_upload(
    session: AsyncSession, *, title: str, data: bytes, content_type: str, tags: str = ""
) -> MediaAsset | None:
    """Persist an uploaded image + its DB row. Returns None on a rejected file
    (not an image, empty, or over the size limit)."""
    ext = ALLOWED_EXT.get((content_type or "").lower())
    if ext is None or not data or len(data) > MAX_BYTES:
        return None
    ensure_media_dir()
    filename = f"{uuid.uuid4().hex}{ext}"
    (MEDIA_DIR / filename).write_bytes(data)
    asset = MediaAsset(
        title=(title or "").strip() or filename,
        filename=filename,
        mime=content_type,
        tags=(tags or "").strip(),
    )
    session.add(asset)
    await session.flush()
    return asset


# Operator-sent files: broader than the image-only bot library (photo/video/voice/doc).
# mime → stored extension (naming); the allow-list is _EXT_KIND below.
_OP_EXT = {
    "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
    "image/gif": ".gif", "image/webp": ".webp",
    "video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm", "video/3gpp": ".3gp",
    "audio/ogg": ".ogg", "audio/opus": ".opus", "audio/mpeg": ".mp3", "audio/mp4": ".m4a",
    "audio/aac": ".aac", "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/webm": ".weba",
    "application/pdf": ".pdf", "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "text/plain": ".txt", "application/zip": ".zip",
}
# extension → send-kind. Also the allow-list when the browser omits a content-type.
_EXT_KIND = {
    ".jpg": "photo", ".jpeg": "photo", ".png": "photo", ".gif": "photo", ".webp": "photo",
    ".mp4": "video", ".mov": "video", ".webm": "video", ".3gp": "video",
    ".ogg": "voice", ".opus": "voice", ".mp3": "voice", ".m4a": "voice",
    ".aac": "voice", ".wav": "voice", ".weba": "voice",
    ".pdf": "document", ".doc": "document", ".docx": "document",
    ".xls": "document", ".xlsx": "document", ".txt": "document", ".zip": "document",
}


def operator_max_mb() -> int:
    """Configured size cap (MB) for operator-sent files / uploaded documents."""
    return max(1, get_settings().operator_max_upload_mb)


def operator_max_bytes() -> int:
    return operator_max_mb() * 1024 * 1024


def _resolve_op_kind(content_type: str, orig_name: str) -> tuple[str, str] | None:
    """Map (mime, filename) → (stored extension, send-kind), or None if not allowed.
    ``kind`` is photo|video|voice|document — how the channel should send the file."""
    mime = (content_type or "").lower()
    ext = _OP_EXT.get(mime) or os.path.splitext(orig_name or "")[1].lower()
    if ext not in _EXT_KIND:
        return None  # not an allowed file type
    # Trust the mime main-type for the kind, falling back to the extension.
    kind = {"image": "photo", "video": "video", "audio": "voice"}.get(mime.split("/")[0])
    return ext, (kind or _EXT_KIND[ext])


def save_operator_file(data: bytes, content_type: str, orig_name: str = "") -> dict | None:
    """Persist a file an operator sends to a client (any allowed type) under the public
    media dir. Returns ``{filename, url, mime, kind}`` or None if empty/too big/blocked.
    For in-memory callers (voice notes); large uploads should use ``save_operator_stream``."""
    if not data or len(data) > operator_max_bytes():
        return None
    resolved = _resolve_op_kind(content_type, orig_name)
    if resolved is None:
        return None
    ext, kind = resolved
    ensure_media_dir()
    filename = f"{uuid.uuid4().hex}{ext}"
    (MEDIA_DIR / filename).write_bytes(data)
    return {
        "filename": filename,
        "url": public_url(filename),
        "mime": content_type or "application/octet-stream",
        "kind": kind,
    }


async def save_operator_stream(upload, *, max_bytes: int | None = None) -> dict | None:
    """Stream a large operator upload to disk in 1 MB chunks — never loading the whole
    file into memory — while enforcing the size cap as it goes. ``upload`` is a Starlette
    UploadFile. Returns ``{filename, url, mime, kind}`` or None (empty / too big / blocked)."""
    cap = max_bytes if max_bytes is not None else operator_max_bytes()
    resolved = _resolve_op_kind(upload.content_type or "", upload.filename or "")
    if resolved is None:
        return None
    ext, kind = resolved
    ensure_media_dir()
    filename = f"{uuid.uuid4().hex}{ext}"
    path = MEDIA_DIR / filename
    size = 0
    too_big = False
    try:
        with path.open("wb") as f:
            while True:
                chunk = await upload.read(1024 * 1024)  # 1 MB at a time
                if not chunk:
                    break
                size += len(chunk)
                if size > cap:
                    too_big = True  # stop; delete AFTER the handle closes (Windows-safe)
                    break
                f.write(chunk)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    if too_big or size == 0:
        path.unlink(missing_ok=True)  # over cap or empty — clean up the partial file
        return None
    # Browser mic recordings arrive as WebM, which WhatsApp rejects (error 131053
    # "Unsupported Audio mime type video/webm"). Transcode voice to OGG/Opus — accepted
    # by WhatsApp AND Telegram, and still playable in the browser panel.
    if kind == "voice" and ext in (".webm", ".weba"):
        transcoded = await asyncio.get_running_loop().run_in_executor(None, _voice_to_ogg, path)
        if transcoded is not None:
            return transcoded  # else keep the original (best-effort; ffmpeg missing/failed)
    return {
        "filename": filename,
        "url": public_url(filename),
        "mime": upload.content_type or "application/octet-stream",
        "kind": kind,
    }


def _voice_to_ogg(src_path) -> dict | None:
    """Transcode a saved voice file to OGG/Opus so channels accept it. Returns a new media
    dict (and removes the original) on success, else None (keep the original).

    TWO PASSES: remux (``-c:a copy``) WebM→OGG first, then re-encode OGG→OGG. Re-encoding the
    browser's WebM/Opus DIRECTLY produced files WhatsApp iOS reported as "media no longer
    available" (its container codec-delay / negative start timestamps confuse ffmpeg's encode);
    the identical re-encode from an OGG intermediate plays fine. Proven by A/B tests:
    every re-encode-from-OGG played; every direct-from-WebM re-encode failed."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    name = f"{uuid.uuid4().hex}.ogg"
    dst = MEDIA_DIR / name
    inter = MEDIA_DIR / f"{uuid.uuid4().hex}.ogg"  # remux intermediate
    try:
        # Pass 1: remux to a clean OGG container (normalizes WebM codec delay). If it fails
        # (e.g. non-Opus source), fall back to encoding straight from the original.
        r1 = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src_path),
             "-vn", "-c:a", "copy", str(inter)],
            capture_output=True, timeout=60, check=False,
        )
        enc_src = inter if (r1.returncode == 0 and inter.exists() and inter.stat().st_size > 0) \
            else src_path
        # Pass 2: re-encode the OGG intermediate to a clean mono voice note.
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(enc_src),
             "-vn", "-ac", "1", "-ar", "48000",
             "-c:a", "libopus", "-b:a", "32k", "-application", "voip", str(dst)],
            capture_output=True, timeout=60, check=False,
        )
        inter.unlink(missing_ok=True)
        if proc.returncode == 0 and dst.exists() and dst.stat().st_size > 0:
            src_path.unlink(missing_ok=True)
            return {"filename": name, "url": public_url(name), "mime": "audio/ogg", "kind": "voice"}
        logger.warning(
            "voice transcode failed rc=%s: %s",
            proc.returncode,
            proc.stderr.decode("utf-8", "replace")[:200],
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("voice transcode error: %s", e)
    inter.unlink(missing_ok=True)
    dst.unlink(missing_ok=True)
    return None


def _sniff_audio_ext(data: bytes) -> str:
    """Best-guess file extension for inbound voice bytes from magic numbers.

    Platforms don't hand us a clean content-type for a voice note (WhatsApp gives a
    token-gated media id, Instagram a CDN URL), so we read the container signature.
    Defaults to ``.ogg`` — WhatsApp voice notes are opus-in-ogg.
    """
    if data[:4] == b"OggS":
        return ".ogg"
    if data[:3] == b"ID3" or data[:2] == b"\xff\xfb":
        return ".mp3"
    if data[4:8] == b"ftyp":  # ISO-BMFF: mp4/m4a/aac container (Instagram voice)
        return ".m4a"
    if data[:4] == b"RIFF":
        return ".wav"
    return ".ogg"


def save_inbound_audio(data: bytes) -> dict | None:
    """Persist a client's inbound voice note so operators can replay it in «Диалоги».

    Returns ``{filename, url, kind: "voice"}`` (served publicly at ``/media/{filename}``)
    or None if empty / over the size limit. Kept separate from ``save_operator_file``
    because the format is sniffed from bytes, not a browser-declared content-type.
    """
    if not data or len(data) > operator_max_bytes():
        return None
    ensure_media_dir()
    filename = f"{uuid.uuid4().hex}{_sniff_audio_ext(data)}"
    (MEDIA_DIR / filename).write_bytes(data)
    return {"filename": filename, "url": public_url(filename), "kind": "voice"}


async def list_media(session: AsyncSession) -> list[MediaAsset]:
    return list(
        (
            await session.execute(
                select(MediaAsset).order_by(MediaAsset.sort_order, MediaAsset.id.desc())
            )
        ).scalars()
    )


def is_picture(asset: MediaAsset) -> bool:
    """Можно ли отправить ассет как фото.

    В медиатеке рядом с картинками лежат ссылки (2ГИС, Instagram) — без файла.
    Отправленная как фото, такая запись превращается в URL «/media/», WhatsApp
    отвечает «Media upload error», и клиент не получает ничего.
    """
    return (asset.kind or "image") == "image" and bool((asset.filename or "").strip())


async def active_media(session: AsyncSession) -> list[MediaAsset]:
    return list(
        (
            await session.execute(
                select(MediaAsset)
                .where(MediaAsset.is_active.is_(True))
                .order_by(MediaAsset.sort_order, MediaAsset.id)
            )
        ).scalars()
    )


async def delete_media(session: AsyncSession, media_id: int) -> bool:
    asset = await session.get(MediaAsset, media_id)
    if asset is None:
        return False
    try:
        (MEDIA_DIR / asset.filename).unlink(missing_ok=True)
    except OSError:
        logger.warning("Could not remove media file %s", asset.filename)
    await session.delete(asset)
    return True


async def search_media(session: AsyncSession, query: str, limit: int = 1) -> list[MediaAsset]:
    """Best-matching active assets for a free-text query (title + tags + trigger overlap)."""
    items = await active_media(session)
    q = _tokens(query)
    if not q or not items:
        return []
    scored = [(it, len(_tokens(f"{it.title} {it.tags} {it.trigger}") & q)) for it in items]
    scored = [(it, s) for it, s in scored if s > 0]
    scored.sort(key=lambda x: x[1], reverse=True)
    return [it for it, _ in scored[:limit]]
