"""LLM client tests — the temperature-unsupported retry (gpt-5.x / o-series)."""

from __future__ import annotations

import asyncio

import httpx

import app.sales.llm as llm_mod
from app.sales.llm import chat_completion

_TEMP_ERR = (
    '{"error":{"message":"Unsupported value: \'temperature\' does not support 0.4 '
    "with this model. Only the default (1) value is supported.\","
    '"type":"invalid_request_error","param":"temperature","code":"unsupported_value"}}'
)
_REPLY = {"choices": [{"message": {"role": "assistant", "content": "привет"}}]}


class _Resp:
    def __init__(self, status: int, *, text: str = "", data: dict | None = None) -> None:
        self.status_code = status
        self.text = text
        self._data = data if data is not None else {}

    def json(self) -> dict:
        return self._data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=None)


def _patch(monkeypatch, responses: list[_Resp], calls: list[dict]) -> None:
    seq = iter(responses)

    class FakeClient:
        def __init__(self, *a, **k) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a) -> bool:
            return False

        async def post(self, url, json, headers):
            calls.append(dict(json))  # snapshot — caller may build a fresh payload
            return next(seq)

    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", FakeClient)


def test_retries_without_temperature_when_unsupported(monkeypatch):
    calls: list[dict] = []
    _patch(
        monkeypatch,
        [
            _Resp(400, text=_TEMP_ERR, data={"error": {"param": "temperature"}}),
            _Resp(200, data=_REPLY),
        ],
        calls,
    )

    cfg = {"base_url": "http://x/v1", "api_key": "k", "model": "gpt-5.5", "temperature": 0.4}

    async def main():
        return await chat_completion(
            messages=[{"role": "user", "content": "hi"}], tools=None, config=cfg
        )

    msg = asyncio.run(main())
    assert msg["content"] == "привет"
    assert len(calls) == 2  # original + retry
    assert "temperature" in calls[0]  # first attempt sent it
    assert "temperature" not in calls[1]  # retry dropped it


def test_no_retry_on_success(monkeypatch):
    calls: list[dict] = []
    _patch(monkeypatch, [_Resp(200, data=_REPLY)], calls)

    cfg = {"base_url": "http://x/v1", "api_key": "k", "model": "gpt-4o", "temperature": 0.4}

    async def main():
        return await chat_completion(
            messages=[{"role": "user", "content": "hi"}], tools=None, config=cfg
        )

    msg = asyncio.run(main())
    assert msg["content"] == "привет"
    assert len(calls) == 1  # no retry when the first call succeeds
    assert calls[0]["temperature"] == 0.4
