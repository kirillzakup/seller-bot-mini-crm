"""Tests for the .env mirror writer (uses a temp path — never the real .env)."""

from __future__ import annotations

import app.services.env_file as env_file


def test_write_env_updates_and_preserves(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    p.write_text("# comment\nBOT_TOKEN=\nBASE_URL=https://x\nLLM_MODEL=old\n", encoding="utf-8")
    monkeypatch.setattr(env_file, "ENV_PATH", p)

    assert env_file.write_env_values({"BOT_TOKEN": "123:ABC", "LLM_MODEL": "gpt-4o-mini"}) is True

    lines = p.read_text(encoding="utf-8").splitlines()
    assert "# comment" in lines  # comments preserved
    assert "BASE_URL=https://x" in lines  # unrelated key untouched
    assert "BOT_TOKEN=123:ABC" in lines  # updated in place
    assert "LLM_MODEL=gpt-4o-mini" in lines
    assert sum(1 for ln in lines if ln.startswith("BOT_TOKEN=")) == 1  # no duplicate


def test_write_env_appends_new_keys(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    p.write_text("BOT_TOKEN=\n", encoding="utf-8")
    monkeypatch.setattr(env_file, "ENV_PATH", p)

    env_file.write_env_values({"ELEVENLABS_API_KEY": "el-key"})
    lines = p.read_text(encoding="utf-8").splitlines()
    assert "ELEVENLABS_API_KEY=el-key" in lines
    assert "BOT_TOKEN=" in lines


def test_write_env_creates_file_when_missing(tmp_path, monkeypatch):
    p = tmp_path / ".env"  # does not exist yet
    monkeypatch.setattr(env_file, "ENV_PATH", p)

    assert env_file.write_env_values({"BOT_TOKEN": "t"}) is True
    assert p.exists()
    assert "BOT_TOKEN=t" in p.read_text(encoding="utf-8")


def test_write_env_empty_is_noop(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    monkeypatch.setattr(env_file, "ENV_PATH", p)

    assert env_file.write_env_values({}) is True
    assert not p.exists()  # nothing created
