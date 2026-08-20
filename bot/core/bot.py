"""The Discord client for the Riyxoen moderation bot."""

from __future__ import annotations

import asyncio
import logging
import signal
import sqlite3

import discord
from discord import app_commands

from bot.configuration.settings import Settings
from bot.core.error_handler import handle_application_error
from bot.core.intents import build_intents
from bot.core.shutdown import ShutdownManager
from bot.database.config_repository import (
    MemoryGuildConfigRepository,
    build_guild_config_repository,
)
from bot.database.repository import MemoryCaseRepository, build_case_repository

logger = logging.getLogger("riyxoen.bot")


class RiyxoenCommandTree(app_commands.CommandTree):
    """Command tree that routes every command error through centralized handling."""

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        await handle_application_error(interaction, error)


class RiyxoenBot(discord.Client):
    """The bot client. Slash-commands only; no prefix-command support."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        super().__init__(intents=build_intents(settings))
        # Plain ``discord.Client`` does not create a command tree (only
        # ``discord.ext.commands.Bot`` does). Slash commands are the bot's
        # only interface, so attach our custom tree here, mirroring what
        # ``Bot.__init__`` does internally.
        self.tree = RiyxoenCommandTree(self)
        # Built in ``setup_hook``; ``None`` until then (messages cannot arrive
        # before setup_hook completes, but guard anyway).
        self.automod = None
        self.prefix = None
        logger.info("intents configured")

    async def setup_hook(self) -> None:
        # Local imports avoid cycles between core.bot and the cogs/services.
        from bot.automod.engine import AutomodEngine
        from bot.cogs.automod import AutomodCog
        from bot.cogs.cases import case_command, cases_command, moderation_history_command
        from bot.cogs.config import ConfigCog
        from bot.cogs.general import help_command, ping
        from bot.cogs.health import HealthCog
        from bot.cogs.moderation import (
            ban,
            kick,
            lock,
            purge,
            slowmode,
            timeout,
            unban,
            unlock,
            warn,
        )
        from bot.permissions.checks import PermissionChecker
        from bot.prefix import PrefixDispatcher
        from bot.services.cases import CaseService
        from bot.services.confirmation import ConfirmationController
        from bot.services.custom_roles import CustomRoleService
        from bot.services.guild_config import GuildConfigService
        from bot.services.moderation import ModerationService

        logger.info("command registration beginning")
        # Database auto-initializes here (schema + migrations) at startup.
        self.case_repository = self._build_case_repository()
        self.case_service = CaseService(self.case_repository)
        self.config_repository = self._build_config_repository()
        self.config_service = GuildConfigService(self.config_repository, settings=self.settings)
        # One shared permission checker for the moderation service and commands;
        # it consults per-guild configuration (moderator/admin roles).
        self.permissions = PermissionChecker(
            self, self.settings, config_service=self.config_service
        )
        self.moderation_service = ModerationService(
            self,
            self.case_service,
            settings=self.settings,
            permissions=self.permissions,
            config_service=self.config_service,
        )
        # Confirmation state machine for dangerous actions (Phase 6).
        self.confirmation_service = ConfirmationController()
        # Custom-role system (Phase 6, ``.el`` prefix commands) — enabled
        # state and the managed role ID live in the per-guild config.
        self.custom_roles = CustomRoleService(self.settings, self.permissions, self.config_service)
        # Prefix (text) commands: ``.el``, ``.ban``, ``.kick``, ``.mute``,
        # ``.unmute``. Requires the message-content intent (on by default).
        self.prefix = PrefixDispatcher(self, self.settings)
        self.automod = AutomodEngine(
            self.settings,
            self.case_service,
            self.moderation_service,
            self.permissions,
            self.config_service,
        )
        # A /config change invalidates the engine's per-guild detector sets
        # so new thresholds apply to the next message.
        self.config_service.add_invalidation_listener(self.automod.invalidate_guild)
        if self.settings.automod.enabled:
            logger.info(
                "automated moderation enabled (per-guild configuration via /config; "
                "detectors active where a guild enables automod)"
            )
        else:
            logger.info("automated moderation disabled (opt in via RIXYOEN_AUTOMOD_ENABLED=1)")
        self.tree.add_command(HealthCog(self))
        self.tree.add_command(ConfigCog(self, self.config_service, self.moderation_service))
        # Phase 8: /automod command group (enable/disable/status/invites/raid)
        # over the same per-guild configuration service as /config.
        self.tree.add_command(AutomodCog(self, self.config_service))
        # Phase 6: moderation commands are top-level for a consistent UX.
        for command in (warn, timeout, kick, ban, unban, purge, slowmode, lock, unlock):
            self.tree.add_command(command)
        self.tree.add_command(ping)
        self.tree.add_command(help_command)
        self.tree.add_command(case_command)
        self.tree.add_command(cases_command)
        self.tree.add_command(moderation_history_command)
        await self.tree.sync()
        logger.info("command registration complete")

    def _build_case_repository(self):
        """Build the SQLite case repository, degrading to memory when unavailable."""
        try:
            return build_case_repository(self.settings.database_path)
        except (sqlite3.Error, OSError):
            logger.exception(
                "could not open case database %s; using in-memory repository "
                "(cases will not survive a restart)",
                self.settings.database_path,
            )
            return MemoryCaseRepository()

    def _build_config_repository(self):
        """Build the SQLite config repository, degrading to memory when unavailable."""
        try:
            return build_guild_config_repository(self.settings.database_path)
        except (sqlite3.Error, OSError):
            logger.exception(
                "could not open config database %s; using in-memory repository "
                "(per-guild configuration will not survive a restart)",
                self.settings.database_path,
            )
            return MemoryGuildConfigRepository()

    async def on_ready(self) -> None:
        user = self.user
        logger.info("bot connected")
        logger.info("Bot connected as %s", user)
        logger.info("Guild count: %d", len(self.guilds))
        logger.info("latency: %.1f ms", self.latency * 1000)
        await self._update_presence()

    async def _update_presence(self) -> None:
        """Set a lightweight presence (server count) — no external monitoring.

        Presence is cosmetic: failures are logged and never affect the bot.
        """
        try:
            await self.change_presence(
                activity=discord.Activity(
                    type=discord.ActivityType.watching,
                    name=f"{len(self.guilds)} servers",
                )
            )
        except Exception:  # noqa: BLE001 - presence must never crash the bot
            logger.warning("could not update presence", exc_info=True)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        """Keep the presence's server count current when the bot joins a guild."""
        await self._update_presence()

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        """Keep the presence's server count current when the bot leaves a guild."""
        await self._update_presence()

    async def on_message(self, message: discord.Message) -> None:
        """Dispatch guild messages to the prefix commands, then automod.

        Prefix commands (``.el``, ``.ban``, ...) are handled first; the
        automated moderation engine then analyzes the message (with its own
        guards: enabled, guild, bot authors, exemptions). Neither path ever
        raises — the client stays responsive even when a handler misbehaves.
        """
        if self.prefix is not None and await self.prefix.handle(message):
            return
        if self.automod is not None:
            await self.automod.handle_message(message)

    async def on_member_join(self, member: discord.Member) -> None:
        """Feed member joins to the automated moderation engine's raid detector.

        Raid protection uses join timestamps only (no message content, no
        new privileged intents — the ``members`` intent is already enabled
        for moderation). The engine is conservative: the default action is an
        ``alert`` to the log channel, never an automatic punishment.
        """
        if self.automod is not None:
            await self.automod.handle_member_join(getattr(member, "guild", None), member)


async def run(settings: Settings) -> int:
    """Run the bot until a shutdown signal or an unrecoverable disconnect."""
    shutdown = ShutdownManager(timeout_seconds=settings.shutdown_timeout_seconds)
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, shutdown.request_stop)
        except NotImplementedError:
            logger.warning("signal handling not supported for %s; ignoring", sig)

    client = RiyxoenBot(settings)
    start_task = asyncio.create_task(client.start(settings.token), name="client-start")
    stop_task = asyncio.create_task(shutdown.wait_for_stop(), name="wait-for-stop")

    exit_code = 0
    try:
        done, _pending = await asyncio.wait(
            {start_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if start_task in done:
            if start_task.cancelled():
                logger.warning("client start was cancelled")
                exit_code = 1
            elif (exc := start_task.exception()) is not None:
                logger.exception("client exited with an error", exc_info=exc)
                exit_code = 1
            else:
                logger.info("client disconnected; shutting down")
        else:
            logger.info("shutdown signal received; closing client")

        if not client.is_closed():
            await shutdown.close_client(client)
    finally:
        for task in (start_task, stop_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(start_task, stop_task, return_exceptions=True)
        logger.info("shutdown complete")
    return exit_code
