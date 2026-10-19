"""JSON API for the native «Диалоги» app: bearer-token auth + core endpoints."""

from __future__ import annotations


def _token(client) -> str:
    r = client.post("/appapi/login", json={"username": "admin", "password": "admin"})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def test_login_rejects_bad_credentials(client):
    r = client.post("/appapi/login", json={"username": "admin", "password": "nope"})
    assert r.status_code == 401


def test_endpoints_require_bearer_token(client):
    assert client.get("/appapi/conversations").status_code == 401
    assert client.get("/appapi/ping").status_code == 401
    bad = client.get("/appapi/conversations", headers={"Authorization": "Bearer garbage"})
    assert bad.status_code == 401


def test_conversations_and_me_with_token(client):
    h = {"Authorization": f"Bearer {_token(client)}"}
    r = client.get("/appapi/me", headers=h)
    assert r.status_code == 200 and r.json()["user"] == "admin"
    r = client.get("/appapi/conversations", headers=h)
    assert r.status_code == 200 and isinstance(r.json()["conversations"], list)
    r = client.get("/appapi/ping", headers=h)
    assert r.status_code == 200 and "max_id" in r.json()


def test_push_register_validates_expo_token(client):
    h = {"Authorization": f"Bearer {_token(client)}"}
    bad = client.post("/appapi/push/register", json={"token": "garbage"}, headers=h)
    assert bad.status_code == 400
    ok = client.post(
        "/appapi/push/register", json={"token": "ExponentPushToken[xxxxxxxx]"}, headers=h
    )
    assert ok.status_code == 200 and ok.json()["ok"] is True


def test_send_and_bot_toggle_flow(client):
    import asyncio

    from app.db.base import AsyncSessionLocal
    from app.db.models import User
    from app.db.models.enums import Channel

    async def _seed() -> int:
        async with AsyncSessionLocal() as s:
            u = User(channel=Channel.TELEGRAM, external_id="appapi-1", first_name="Тест")
            s.add(u)
            await s.commit()
            return u.id

    uid = asyncio.run(_seed())
    h = {"Authorization": f"Bearer {_token(client)}"}
    # messages for the fresh chat
    r = client.get(f"/appapi/conversations/{uid}/messages", headers=h)
    assert r.status_code == 200 and r.json()["user"]["user_id"] == uid
    # pause the bot
    r = client.post(f"/appapi/conversations/{uid}/bot", json={"paused": True}, headers=h)
    assert r.status_code == 200 and r.json()["bot_paused"] is True
    # send (no live messenger in tests → delivery may fail, but the turn is recorded)
    r = client.post(f"/appapi/conversations/{uid}/send", json={"text": "Здравствуйте!"}, headers=h)
    assert r.status_code == 200
    assert any(m["text"] == "Здравствуйте!" for m in r.json()["messages"])


def test_login_throttled_after_repeated_failures(client):
    """Перебор пароля должен упираться в блокировку, а не идти со скоростью сети."""
    from app.appapi.routes import _login_throttle

    _login_throttle.reset("testclient")
    try:
        codes = []
        for _ in range(_login_throttle.max_attempts + 2):
            r = client.post("/appapi/login", json={"username": "admin", "password": "неверный"})
            codes.append(r.status_code)

        assert codes[0] == 401, "первые попытки отвечают обычным отказом"
        assert 429 in codes, "после серии промахов вход обязан блокироваться"
        blocked = client.post(
            "/appapi/login", json={"username": "admin", "password": "admin"}
        )
        assert blocked.status_code == 429, "блокировка действует и на верный пароль"
        assert "Retry-After" in blocked.headers
    finally:
        _login_throttle.reset("testclient")


def test_successful_login_clears_failed_attempts(client):
    """Удачный вход сбрасывает счётчик промахов — иначе оператор блокирует сам себя."""
    from app.appapi.routes import _login_throttle

    _login_throttle.reset("testclient")
    try:
        client.post("/appapi/login", json={"username": "admin", "password": "мимо"})
        ok = client.post("/appapi/login", json={"username": "admin", "password": "admin"})
        assert ok.status_code == 200
        assert _login_throttle.blocked_for("testclient") == 0
        assert "testclient" not in _login_throttle._fails
    finally:
        _login_throttle.reset("testclient")


def test_login_with_cyrillic_password_returns_401_not_500(client):
    """Кириллица в пароле — это неверный пароль, а не падение сервера.

    Регрессия: secrets.compare_digest на строках требует ASCII и бросал
    TypeError, из-за чего запрос завершался 500-й ошибкой.
    """
    from app.appapi.routes import _login_throttle

    _login_throttle.reset("testclient")
    try:
        r = client.post("/appapi/login", json={"username": "admin", "password": "Пароль123"})
        assert r.status_code == 401
        r2 = client.post("/appapi/login", json={"username": "админ", "password": "admin"})
        assert r2.status_code == 401
    finally:
        _login_throttle.reset("testclient")
