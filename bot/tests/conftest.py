"""Shared test fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def clean_bot_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Remove bot-related environment variables before every test.

    Keeps tests hermetic regardless of the developer's shell environment.
    """

    monkeypatch.chdir(tmp_path)

    for key in (
        "DISCORD_TOKEN",
        "RIYXOEN_LOG_LEVEL",
        "RIYXOEN_LOG_FILE",
        "RIYXOEN_SHUTDOWN_TIMEOUT_SECONDS",
        "RIYXOEN_ENABLE_MESSAGE_CONTENT_INTENT",
        "RIYXOEN_MODERATOR_ROLE_IDS",
        "RIYXOEN_LOG_CHANNEL_ID",
        "RIYXOEN_NOTIFY_USERS",
        "RIYXOEN_MAX_PURGE_AMOUNT",
        "RIYXOEN_DATABASE_PATH",
        "RIYXOEN_COMMAND_PREFIX",
        "RIYXOEN_AUTOMOD_ENABLED",
        "RIYXOEN_AUTOMOD_SPAM_THRESHOLD",
        "RIYXOEN_AUTOMOD_SPAM_WINDOW_SECONDS",
        "RIYXOEN_AUTOMOD_SPAM_ACTION",
        "RIYXOEN_AUTOMOD_DUPLICATE_THRESHOLD",
        "RIYXOEN_AUTOMOD_DUPLICATE_WINDOW_SECONDS",
        "RIYXOEN_AUTOMOD_DUPLICATE_ACTION",
        "RIYXOEN_AUTOMOD_MENTION_USER_THRESHOLD",
        "RIYXOEN_AUTOMOD_MENTION_ROLE_THRESHOLD",
        "RIYXOEN_AUTOMOD_MENTION_TOTAL_THRESHOLD",
        "RIYXOEN_AUTOMOD_MENTION_EVERYONE_THRESHOLD",
        "RIYXOEN_AUTOMOD_MENTION_ACTION",
        "RIYXOEN_AUTOMOD_LINK_ACTION",
        "RIYXOEN_AUTOMOD_ALLOWED_DOMAINS",
        "RIYXOEN_AUTOMOD_BLOCKED_TERMS",
        "RIYXOEN_AUTOMOD_BLOCKED_TERMS_SUBSTRING",
        "RIYXOEN_AUTOMOD_WORD_FILTER_ACTION",
        "RIYXOEN_AUTOMOD_EXEMPT_USER_IDS",
        "RIYXOEN_AUTOMOD_EXEMPT_ROLE_IDS",
        "RIYXOEN_AUTOMOD_EXEMPT_CHANNEL_IDS",
        "RIYXOEN_AUTOMOD_ENFORCEMENT_COOLDOWN_SECONDS",
        "RIYXOEN_AUTOMOD_WARNING_WINDOW_SECONDS",
        "RIYXOEN_AUTOMOD_ESCALATION",
        "RIYXOEN_AUTOMOD_INVITE_ACTION",
        "RIYXOEN_AUTOMOD_INVITE_ALLOWED_CODES",
        "RIYXOEN_AUTOMOD_RAID_JOIN_THRESHOLD",
        "RIYXOEN_AUTOMOD_RAID_WINDOW_SECONDS",
        "RIYXOEN_AUTOMOD_RAID_ACTION",
    ):
        monkeypatch.delenv(key, raising=False)
    # Clean up any test databases
    import shutil
    data_dir = tmp_path / "data"
    if data_dir.exists():
        shutil.rmtree(data_dir, ignore_errors=True)


@pytest.fixture
def fake_token() -> str:
    """A token-shaped but obviously fake string (never a real Discord token)."""
    return "F" * 24 + "." + "a" * 10 + "." + "b" * 24
