"""Runtime settings for the bot."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from bot.configuration.automod import AutomodSettings


@dataclass(frozen=True, slots=True)
class Settings:
    """Validated runtime configuration.

    All values are resolved from the environment (and an optional ``.env``
    file) by :func:`bot.configuration.loader.load_settings`. A ``Settings``
    instance is always valid — construction happens only after validation.
    """

    token: str
    log_level: int = logging.INFO
    log_file: Path | None = None
    shutdown_timeout_seconds: float = 10.0
    #: Message-content intent. **On by default since Phase 6**: the prefix
    #: commands (``.el``, ``.ban``, ``.kick``, ``.mute``, ``.unmute``) and
    #: the automated-moderation detectors genuinely require message text, so
    #: the privileged intent is needed for the features this bot ships. It
    #: must also be enabled in the Discord developer portal or the gateway
    #: rejects the connection (see ``bot.core.intents.build_intents``).
    enable_message_content_intent: bool = True
    #: Prefix for the legacy text commands (``.el``, ``.ban``, ...).
    command_prefix: str = "."
    #: Discord role IDs that grant full moderation access (any command).
    moderator_role_ids: tuple[int, ...] = ()
    #: Optional Discord channel that receives a case summary per action.
    log_channel_id: int | None = None
    #: Whether punished users receive a best-effort DM notification.
    notify_users: bool = True
    #: Maximum number of messages a single purge may delete.
    max_purge_amount: int = 100
    #: Local SQLite file for case records (git-ignored).
    database_path: Path = Path("data/cases.db")
    #: Automated moderation engine configuration (Phase 4; opt-in).
    automod: AutomodSettings = field(default_factory=AutomodSettings)
