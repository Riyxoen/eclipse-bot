"""Discord Gateway intents for the bot.

Least-privilege by default: intents are built from :meth:`discord.Intents.none`
and only the intents the platform actually needs are enabled.
"""

from __future__ import annotations

import discord

from bot.configuration.settings import Settings


def build_intents(settings: Settings) -> discord.Intents:
    """Build the :class:`discord.Intents` for the bot from validated settings.

    - ``guilds`` (non-privileged) — required for guild context, member
      lookups, and guild slash commands. Always enabled.
    - ``members`` (privileged — must be enabled in the Discord developer
      portal) — required for member caching, role-hierarchy checks, and
      moderation target validation. Always enabled.
    - ``message_content`` (privileged — must be enabled in the Discord
      developer portal) — **on by default since Phase 6**. The prefix
      commands (``.el enable/rename/color``, ``.ban``, ``.kick``, ``.mute``,
      ``.unmute``) and the Phase 4 automated-moderation detectors genuinely
      require message text, so the intent is enabled for the features this
      bot ships; ``RIYXOEN_ENABLE_MESSAGE_CONTENT_INTENT=0`` opts out (the
      prefix commands then no-op with a logged warning). The operator must
      also enable the intent in the Discord developer portal or the gateway
      rejects the connection at startup.

    All other intents (presences, voice states, message reactions, ...) are
    left disabled — a moderation bot does not need them.
    """
    intents = discord.Intents.none()
    intents.guilds = True
    intents.members = True
    intents.message_content = settings.enable_message_content_intent or settings.automod.enabled
    return intents
