"""Voice (ElevenLabs) tests — policy, STT/TTS over mocked httpx, and the speak path."""

from __future__ import annotations

import asyncio
import random

import httpx

import app.sales.ai_manager as ai
from app.channels.meta import parse_webhook
from app.db.base import AsyncSessionLocal
from app.db.models import User
from app.db.models.enums import Channel
from app.services.settings_service import set_setting
from app.voice import elevenlabs as voice

# --------------------------- test doubles ---------------------------


class _Resp:
    def __init__(self, *, status_code=200, json_data=None, content=b"") -> None:
        self.status_code = status_code
        self._json = json_data or {}
        self.content = content
        self.text = ""

    def json(self) -> dict:
        return self._json


def _patch_httpx(monkeypatch, resp: _Resp, captured: dict) -> None:
    class FakeClient:
        def __init__(self, *a, **k) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a) -> bool:
            return False

        async def post(self, url, **kwargs):
            captured["url"] = url
            captured["kwargs"] = kwargs
            return resp

        async def get(self, url, **kwargs):
            captured.setdefault("gets", []).append(url)
            return resp

    monkeypatch.setattr(voice.httpx, "AsyncClient", FakeClient)


class FakeVoiceMessenger:
    def __init__(self) -> None:
        self.channel = Channel.TELEGRAM
        self.texts: list[str] = []
        self.voices: list[tuple[str, bytes]] = []

    async def send_text(self, external_id: str, text: str) -> None:
        self.texts.append(text)

    async def send_voice(self, external_id: str, audio: bytes, *, mime: str, filename: str) -> bool:
        self.voices.append((mime, audio))
        return True


# --------------------------- policy ---------------------------


def test_should_speak_modes():
    base = {"enabled": True, "api_key": "k"}
    assert voice.should_speak({**base, "reply_mode": "mirror"}, was_voice=True, user_text="hi")
    assert not voice.should_speak({**base, "reply_mode": "mirror"}, was_voice=False, user_text="hi")
    assert voice.should_speak({**base, "reply_mode": "always"}, was_voice=False, user_text="hi")
    assert voice.should_speak(
        {**base, "reply_mode": "on_request"}, was_voice=False, user_text="ответь голосом"
    )
    assert not voice.should_speak(
        {**base, "reply_mode": "on_request"}, was_voice=False, user_text="сколько стоит"
    )
    assert not voice.should_speak({**base, "reply_mode": "off"}, was_voice=True, user_text="hi")
    # Disabled / no key never speaks
    assert not voice.should_speak({"enabled": False, "api_key": "k"}, was_voice=True, user_text="x")
    assert not voice.should_speak({"enabled": True, "api_key": ""}, was_voice=True, user_text="x")


# --------------------------- STT ---------------------------


def test_transcribe_parses_text(monkeypatch):
    captured: dict = {}
    _patch_httpx(monkeypatch, _Resp(json_data={"text": "привет мир"}), captured)

    async def main():
        cfg = {"api_key": "key", "stt_model": "scribe_v1"}
        return await voice.transcribe(b"AUDIOBYTES", filename="voice.ogg", cfg=cfg)

    text = asyncio.run(main())
    assert text == "привет мир"
    assert captured["url"].endswith("/speech-to-text")
    assert captured["kwargs"]["headers"]["xi-api-key"] == "key"
    assert "file" in captured["kwargs"]["files"]


def test_transcribe_returns_none_without_key():
    async def main():
        return await voice.transcribe(b"x", filename="a.ogg", cfg={"api_key": ""})

    assert asyncio.run(main()) is None


# --------------------------- TTS ---------------------------


def test_synthesize_reply_mp3_fallback_without_ffmpeg(monkeypatch):
    captured: dict = {}
    _patch_httpx(monkeypatch, _Resp(content=b"MP3BYTES"), captured)
    monkeypatch.setattr(voice.shutil, "which", lambda _name: None)  # no ffmpeg

    async def main():
        cfg = {"api_key": "k", "voice_id": "v", "format": "voice"}
        return await voice.synthesize_reply("Здравствуйте!", cfg=cfg)

    reply = asyncio.run(main())
    assert reply is not None
    assert reply.mime == "audio/mpeg"  # fell back to mp3 since ffmpeg is absent
    assert reply.data == b"MP3BYTES"
    assert "/text-to-speech/v" in captured["url"]


def test_synthesize_reply_none_without_voice_id():
    async def main():
        return await voice.synthesize_reply("hi", cfg={"api_key": "k", "voice_id": ""})

    assert asyncio.run(main()) is None


def test_to_ogg_opus_without_ffmpeg(monkeypatch):
    monkeypatch.setattr(voice.shutil, "which", lambda _name: None)
    assert voice.to_ogg_opus(b"MP3") is None


# --------------------------- Meta inbound audio ---------------------------


def test_parse_webhook_detects_whatsapp_audio():
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "contacts": [{"wa_id": "777", "profile": {"name": "Аско"}}],
                            "messages": [
                                {"from": "777", "type": "audio", "audio": {"id": "media-1"}}
                            ],
                        }
                    }
                ]
            }
        ],
    }
    msgs = parse_webhook(payload)
    assert len(msgs) == 1
    assert msgs[0].has_audio
    assert msgs[0].audio_ref == {"kind": "audio", "file_id": "media-1"}


def test_parse_webhook_detects_instagram_audio():
    payload = {
        "object": "instagram",
        "entry": [
            {
                "messaging": [
                    {
                        "sender": {"id": "ig-9"},
                        "message": {
                            "attachments": [
                                {"type": "audio", "payload": {"url": "https://cdn/x.mp4"}}
                            ]
                        },
                    }
                ]
            }
        ],
    }
    msgs = parse_webhook(payload)
    assert len(msgs) == 1
    assert msgs[0].has_audio
    assert msgs[0].audio_ref["url"] == "https://cdn/x.mp4"


# --------------------------- speak path (integration) ---------------------------


async def _make_user(session) -> User:
    n = random.randint(1, 10**9)
    user = User(channel=Channel.TELEGRAM, external_id=str(n), telegram_id=n, first_name="Тест")
    session.add(user)
    await session.flush()
    await set_setting(
        session, "llm", {"base_url": "http://fake/v1", "api_key": "k", "model": "m"}
    )
    return user


def test_handle_text_speaks_when_requested(monkeypatch):
    async def main():
        async with AsyncSessionLocal() as s:
            user = await _make_user(s)
            await set_setting(
                s,
                "voice",
                {"enabled": True, "api_key": "k", "voice_id": "v", "reply_mode": "always"},
            )

            async def fake_cc(*, messages, tools, config):
                return {"role": "assistant", "content": "Готово, спасибо!"}

            async def fake_syn(text, *, cfg, instagram=False):
                return voice.VoiceReply(data=b"AUD", mime="audio/ogg", filename="r.ogg")

            monkeypatch.setattr(ai, "chat_completion", fake_cc)
            monkeypatch.setattr(voice, "synthesize_reply", fake_syn)

            msg = FakeVoiceMessenger()
            await ai.handle_text(
                s, user=user, text="привет", messenger=msg, reply_voice=True
            )
            await s.commit()

            assert msg.voices and msg.voices[-1][0] == "audio/ogg"
            assert not msg.texts  # no link in reply → voice only

    asyncio.run(main())


def test_handle_text_text_only_when_not_voice(monkeypatch):
    async def main():
        async with AsyncSessionLocal() as s:
            user = await _make_user(s)

            async def fake_cc(*, messages, tools, config):
                return {"role": "assistant", "content": "Текстовый ответ"}

            monkeypatch.setattr(ai, "chat_completion", fake_cc)

            msg = FakeVoiceMessenger()
            # reply_voice=False (mirror on a text message) → must stay text
            await ai.handle_text(
                s, user=user, text="привет", messenger=msg, reply_voice=False
            )
            await s.commit()

            assert msg.texts and not msg.voices

    asyncio.run(main())


# --------------------------- language handling ---------------------------


def test_clean_lang():
    assert voice._clean_lang("ru") == "ru"
    assert voice._clean_lang("  RU ") == "ru"
    assert voice._clean_lang("eng") == "eng"
    assert voice._clean_lang("ru/kz") == ""  # the real bug input
    assert voice._clean_lang("en-US") == ""
    assert voice._clean_lang("русский") == ""
    assert voice._clean_lang("") == ""
    assert voice._clean_lang(None) == ""


def test_synthesize_retries_without_language(monkeypatch):
    calls: list[dict] = []
    err = _Resp(status_code=400, json_data={"detail": {"status": "unsupported_language"}})
    ok = _Resp(status_code=200, content=b"MP3BYTES")
    seq = iter([err, ok])

    class FakeClient:
        def __init__(self, *a, **k) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a) -> bool:
            return False

        async def post(self, url, **kwargs):
            calls.append(dict(kwargs.get("json") or {}))
            return next(seq)

    monkeypatch.setattr(voice.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(voice.shutil, "which", lambda _n: None)  # no ffmpeg → mp3 reply

    async def main():
        cfg = {"api_key": "k", "voice_id": "v", "format": "voice", "language": "xx"}
        return await voice.synthesize_reply("Привет", cfg=cfg)

    reply = asyncio.run(main())
    assert reply is not None and reply.data == b"MP3BYTES"
    assert len(calls) == 2  # original + retry
    assert "language_code" in calls[0]  # first attempt sent the language
    assert "language_code" not in calls[1]  # retry dropped it


# ── Инцидент 04.08.2026: клиентка прислала два голосовых, ElevenLabs на секунду
# отдал ошибку — бот дважды ответил «не расслышала» и потерял оба вопроса.
# Те же файлы расшифровались с первой попытки часом позже: сбой был временный.


def _patch_httpx_sequence(monkeypatch, responses: list, calls: dict) -> None:
    """Отдаёт ответы по очереди — чтобы проверить именно повторные попытки."""

    class FakeClient:
        def __init__(self, *a, **k) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a) -> bool:
            return False

        async def post(self, url, **kwargs):
            calls["n"] = calls.get("n", 0) + 1
            item = responses[min(calls["n"] - 1, len(responses) - 1)]
            if isinstance(item, Exception):
                raise item
            return item

    monkeypatch.setattr(voice.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(voice.asyncio, "sleep", _no_sleep)


async def _no_sleep(_seconds):
    return None


def test_transcribe_retries_transient_failure(monkeypatch):
    """Таймаут и 500 — не повод терять сообщение клиента."""
    calls: dict = {}
    _patch_httpx_sequence(
        monkeypatch,
        [
            httpx.ReadTimeout("timed out"),
            _Resp(status_code=500),
            _Resp(json_data={"text": "покажите тёмный и светлый"}),
        ],
        calls,
    )
    out = asyncio.run(voice.transcribe(b"AUDIO", filename="voice.ogg", cfg={"api_key": "k"}))
    assert out == "покажите тёмный и светлый"
    assert calls["n"] == 3, "должно быть ровно три попытки"


def test_transcribe_retries_rate_limit(monkeypatch):
    calls: dict = {}
    _patch_httpx_sequence(
        monkeypatch, [_Resp(status_code=429), _Resp(json_data={"text": "цена какая"})], calls
    )
    assert asyncio.run(
        voice.transcribe(b"AUDIO", filename="voice.ogg", cfg={"api_key": "k"})
    ) == "цена какая"
    assert calls["n"] == 2


def test_transcribe_does_not_retry_permanent_error(monkeypatch):
    """400 на битом аудио повторять бессмысленно — клиент только ждёт дольше."""
    calls: dict = {}
    _patch_httpx_sequence(monkeypatch, [_Resp(status_code=400)], calls)
    out = asyncio.run(voice.transcribe(b"AUDIO", filename="voice.ogg", cfg={"api_key": "k"}))
    assert out is None
    assert calls["n"] == 1


def test_transcribe_gives_up_after_all_attempts(monkeypatch):
    calls: dict = {}
    _patch_httpx_sequence(monkeypatch, [_Resp(status_code=503)], calls)
    out = asyncio.run(voice.transcribe(b"AUDIO", filename="voice.ogg", cfg={"api_key": "k"}))
    assert out is None
    assert calls["n"] == voice._STT_ATTEMPTS
