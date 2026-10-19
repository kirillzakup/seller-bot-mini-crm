"""OpenAI-compatible Chat Completions client (httpx).

Works with OpenAI, DeepSeek, OpenRouter, Gemini (OpenAI-compat) and local Ollama
— anything exposing ``POST {base_url}/chat/completions``. Kept tiny and explicit
so it's easy to read and swap.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

logger = logging.getLogger(__name__)


class LLMNotConfigured(Exception):
    """Raised when base_url/api_key are missing."""


class LLMError(Exception):
    """Raised on transport/HTTP errors talking to the model."""


def _is_temperature_unsupported(resp: httpx.Response) -> bool:
    """True if a 400 is specifically about an unsupported ``temperature`` value.

    gpt-5.x and the o-series reject a custom temperature (only the default is
    allowed); we detect that case to retry without the parameter.
    """
    try:
        err = resp.json().get("error", {})
    except ValueError:
        err = {}
    if err.get("param") == "temperature":
        return True
    body = (resp.text or "").lower()
    return "temperature" in body and "unsupported" in body


async def chat_completion(
    *,
    messages: list[dict],
    tools: list[dict] | None,
    config: dict,
    response_format: dict | None = None,
) -> dict:
    """Call the chat completions endpoint and return the assistant message dict.

    The returned dict may contain ``content`` and/or ``tool_calls``.

    ``response_format`` (``{"type": "json_object"}``) заставляет API вернуть
    синтаксически валидный JSON. Нужен там, где ответ разбирается машиной:
    на длинных ответах модель иначе срывается в текст с пояснениями или
    ставит сырые переводы строк внутри значений, и разбор падает.
    Если эндпоинт параметр не понимает — повторяем запрос без него.
    """
    base_url = (config.get("base_url") or "").rstrip("/")
    api_key = config.get("api_key") or ""
    model = config.get("model") or "gpt-5.5"
    temperature = float(config.get("temperature", 0.4))

    if not base_url or not api_key:
        raise LLMNotConfigured("LLM не настроен: укажите base_url и api_key в настройках")

    payload: dict = {"model": model, "messages": messages, "temperature": temperature}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    if response_format:
        payload["response_format"] = response_format

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    url = f"{base_url}/chat/completions"

    data = None
    for attempt in (1, 2):  # one transparent retry on transient failures
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
                resp = await client.post(url, json=payload, headers=headers)
                # Some models (gpt-5.x, o-series) allow only the default temperature.
                # Retry once without it instead of failing the whole reply.
                if resp.status_code == 400 and _is_temperature_unsupported(resp):
                    payload = {k: v for k, v in payload.items() if k != "temperature"}
                    resp = await client.post(url, json=payload, headers=headers)
                # Не каждый совместимый эндпоинт понимает response_format —
                # снимаем его и пробуем снова, вместо того чтобы терять весь ответ.
                if resp.status_code == 400 and "response_format" in payload:
                    payload.pop("response_format", None)
                    logger.warning("LLM: эндпоинт не принял response_format — повтор без него")
                    resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
            break
        except httpx.HTTPError as e:
            # Timeouts stringify to "" — keep the class name so logs/alerts are never blank.
            err = f"{type(e).__name__}: {e}".rstrip(": ")
            transient = isinstance(e, httpx.TransportError) or (
                isinstance(e, httpx.HTTPStatusError) and e.response.status_code >= 500
            )
            if attempt == 1 and transient:
                logger.warning("LLM request failed (%s) — retrying once", err)
                await asyncio.sleep(1.0)
                continue
            logger.error("LLM request failed: %s", err)
            raise LLMError(err) from e

    usage = data.get("usage") or {}
    if usage:
        cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
        logger.info(
            "💸 llm usage [%s]: in=%s (cached %s) out=%s",
            model, usage.get("prompt_tokens"), cached, usage.get("completion_tokens"),
        )
    try:
        return data["choices"][0]["message"]
    except (KeyError, IndexError) as e:
        raise LLMError(f"Unexpected LLM response: {data}") from e
