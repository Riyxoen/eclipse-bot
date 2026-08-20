"""Tests for pre-flight startup validation."""

from __future__ import annotations

from pathlib import Path

import pytest
from bot.configuration.settings import Settings
from bot.core.startup import check_environment, run_preflight


def _settings(
    token: str,
    log_file: Path | None = None,
    database_path: Path | None = None,
) -> Settings:
    return Settings(token=token, log_file=log_file, database_path=database_path)


def test_preflight_passes_with_valid_settings(tmp_path: Path, fake_token: str) -> None:
    log_file = tmp_path / "logs" / "bot.log"
    db_path = tmp_path / "data" / "cases.db"
    assert run_preflight(_settings(fake_token, log_file, db_path)) == []
    assert db_path.exists()  # database initialized during pre-flight


def test_preflight_flags_missing_token(tmp_path: Path) -> None:
    problems = run_preflight(_settings("", database_path=tmp_path / "cases.db"))
    assert any("DISCORD_TOKEN" in problem for problem in problems)


def test_preflight_flags_placeholder_token(tmp_path: Path) -> None:
    problems = run_preflight(
        _settings("your_discord_bot_token_here", database_path=tmp_path / "cases.db")
    )
    assert any("placeholder" in problem for problem in problems)


def test_preflight_flags_uncreatable_log_dir(tmp_path: Path, fake_token: str) -> None:
    blocker = tmp_path / "logs"
    blocker.write_text("i am a file, not a directory", encoding="utf-8")

    problems = run_preflight(_settings(fake_token, blocker / "bot.log", tmp_path / "cases.db"))

    assert any("log" in problem for problem in problems)


def test_preflight_flags_unopenable_database(tmp_path: Path, fake_token: str) -> None:
    # A directory where the database file should be makes SQLite fail.
    blocker = tmp_path / "cases.db"
    blocker.mkdir()

    problems = run_preflight(_settings(fake_token, database_path=blocker))

    assert any("database" in problem for problem in problems)


def test_check_environment_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_token: str
) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", fake_token)
    # Point the database at a temp path so the check never touches ./data.
    monkeypatch.setenv("RIYXOEN_DATABASE_PATH", str(tmp_path / "cases.db"))
    assert check_environment() == 0


def test_check_environment_failure_when_token_missing() -> None:
    assert check_environment() == 1
