"""Tests for environment loading (``.env`` parsing, precedence)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from bot.configuration.env import load_env_file


def test_missing_env_file_is_not_an_error(tmp_path: Path) -> None:
    load_env_file(tmp_path / "does-not-exist.env")  # must not raise


def test_env_file_populates_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("DISCORD_TOKEN=from.file.token\n", encoding="utf-8")
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)

    load_env_file(env_file)

    assert os.environ.get("DISCORD_TOKEN") == "from.file.token"


def test_existing_environment_wins_over_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("DISCORD_TOKEN=from.file.token\n", encoding="utf-8")
    monkeypatch.setenv("DISCORD_TOKEN", "from.environment.token")

    load_env_file(env_file)

    assert os.environ["DISCORD_TOKEN"] == "from.environment.token"


def test_env_file_handles_comments_and_quotes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        '# a comment\nDISCORD_TOKEN="quoted.token"\nRIYXOEN_LOG_LEVEL=INFO # trailing\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    monkeypatch.delenv("RIYXOEN_LOG_LEVEL", raising=False)

    load_env_file(env_file)

    assert os.environ["DISCORD_TOKEN"] == "quoted.token"
    assert os.environ["RIYXOEN_LOG_LEVEL"] == "INFO"
