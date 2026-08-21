"""Prefix (text) commands for Eclipse.

The bot's primary interface is slash commands, but a set of text commands
uses a configurable prefix (``·`` by default):

    ·cr enable | ·cr rename <name> | ·cr color <hex>
    ·warn @user <reason> | ·warnings @user | ·clearwarnings @user
    ·modhistory @user [page]
    ·ban @user <reason> | ·kick @user <reason>
    ·mute @user <duration> <reason> | ·unmute @user <reason>
    ·untimeout @user [reason]
    ·purge <amount> | ·slowmode <seconds>
    ·lockdown | ·unlockdown
    ·afk [message]
    ·jail setup | ·jail @user [reason] | ·unjail @user
    ·help

The prefix is per-guild: an administrator can change it with ``/config
prefix`` and the dispatcher honors it per message.

Handlers stay thin: they parse the message, delegate to the service layer,
and format safe responses. All permission, hierarchy, and state checks live
in the services — never here.

Safety:
- Prefix commands require the ``message_content`` intent.
- Only guild messages are processed; bots are ignored.
- Every failure is caught and surfaced as a safe message.
- Moderation actions reuse the existing case/audit/log-channel pipeline.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import discord

from bot.configuration.settings import Settings
from bot.moderation.actions import ACTION_VIEW_CASES, ACTION_WARN
from bot.moderation.cases import STATUS_CLEARED, STATUS_SUCCESS, utc_now
from bot.moderation.errors import (
    InvalidTargetError,
    ModerationError,
)
from bot.moderation.response import format_case_list, format_case_response
from bot.moderation.validation import (
    parse_duration,
    validate_purge_amount,
    validate_slowmode_duration,
)
from bot.services.custom_roles import CustomRoleService
from bot.services.moderation import ModerationService
from bot.utilities.embeds import (
    afk_embed,
    custom_role_embed,
    error_embed,
    info_embed,
    jail_embed,
    moderation_action_embed,
    success_embed,
)

logger = logging.getLogger("riyxoen.prefix")

#: Anti-abuse: at most this many prefix commands per user per window.
PREFIX_COMMAND_LIMIT = 10
#: Anti-abuse window (seconds) for :data:`PREFIX_COMMAND_LIMIT`.
PREFIX_COMMAND_WINDOW_SECONDS = 5
#: Upper bound on tracked users (defense against unbounded growth).
PREFIX_MAX_TRACKED_USERS = 10_000

#: Matches a user mention like ``<@123456789012345678>`` (also handles the
#: nickname form ``<@!...>`` which older clients emitted).
_MENTION_PATTERN = re.compile(r"<@!?(\d+)>")

#: Matches the exact bot prefix followed by a command word.
_COMMAND_PATTERN = re.compile(r"^([A-Za-z]+)(?:\s|$)")


class PrefixRateLimiter:
    """Bounded in-memory rate limiter for prefix commands (anti-abuse)."""

    def __init__(
        self,
        *,
        limit: int = PREFIX_COMMAND_LIMIT,
        window_seconds: int = PREFIX_COMMAND_WINDOW_SECONDS,
        max_users: int = PREFIX_MAX_TRACKED_USERS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.limit = max(limit, 1)
        self.window = timedelta(seconds=max(window_seconds, 1))
        self.max_users = max(max_users, 1)
        self._clock = clock or utc_now
        self._timestamps: OrderedDict[int, deque[datetime]] = OrderedDict()

    def __len__(self) -> int:
        return len(self._timestamps)

    def allow(self, user_id: int) -> bool:
        """Record a command attempt and return whether it may proceed."""
        now = self._clock()
        cutoff = now - self.window
        stamps = self._timestamps.get(user_id)
        if stamps is None:
            if len(self._timestamps) >= self.max_users:
                self._evict_oldest()
            stamps = deque(maxlen=self.limit + 1)
            self._timestamps[user_id] = stamps
        while stamps and stamps[0] < cutoff:
            stamps.popleft()
        if len(stamps) >= self.limit:
            return False
        stamps.append(now)
        return True

    def prune(self) -> None:
        """Drop users whose timestamps all fall outside the window."""
        cutoff = self._clock() - self.window
        for user_id in list(self._timestamps):
            stamps = self._timestamps[user_id]
            while stamps and stamps[0] < cutoff:
                stamps.popleft()
            if not stamps:
                del self._timestamps[user_id]

    def _evict_oldest(self) -> None:
        oldest = next(iter(self._timestamps))
        del self._timestamps[oldest]


@dataclass(frozen=True, slots=True)
class ParsedCommand:
    """A parsed prefix command: ``name``, optional ``subcommand``, and the
    raw remaining argument string."""

    name: str
    subcommand: str | None
    arguments: str


def parse_prefix_command(content: str, prefix: str) -> ParsedCommand | None:
    """Parse ``content`` into a :class:`ParsedCommand`, or ``None`` when the
    message is not a prefix command."""
    if not content or not prefix:
        return None
    if not content.startswith(prefix):
        return None
    rest = content[len(prefix) :].strip()
    match = _COMMAND_PATTERN.match(rest)
    if match is None:
        return None
    name = match.group(1).lower()
    remainder = rest[match.end() :].strip()
    parts = remainder.split(maxsplit=1)
    if name in ("el", "cr") and parts:
        subcommand = parts[0].lower()
        arguments = parts[1] if len(parts) > 1 else ""
        return ParsedCommand(name=name, subcommand=subcommand, arguments=arguments)
    if name == "jail" and parts and parts[0].lower() == "setup":
        return ParsedCommand(name=name, subcommand="setup", arguments="")
    return ParsedCommand(name=name, subcommand=None, arguments=remainder)


def parse_member_id(arguments: str) -> int | None:
    """Extract the leading user-mention ID from ``arguments``, or ``None``."""
    match = _MENTION_PATTERN.match(arguments.strip())
    if match is None:
        return None
    return int(match.group(1))


def split_target_and_reason(arguments: str) -> tuple[str | None, str]:
    """Split ``arguments`` into ``(member_id_text, reason)``."""
    stripped = arguments.strip()
    match = _MENTION_PATTERN.match(stripped)
    if match is None:
        return None, stripped
    reason = stripped[match.end() :].strip()
    return match.group(1), reason


def format_moderation_reply(record, *, target_label: str, moderator_label: str) -> str:
    """The case summary for prefix moderation commands."""
    text = format_case_response(record, target_label=target_label, moderator_label=moderator_label)
    if (record.metadata or {}).get("dm_delivered") is False:
        text += "\n_Note: the user could not be DM'd (their DMs may be closed)._"
    return text


class PrefixDispatcher:
    """Routes guild text messages starting with the configured prefix."""

    def __init__(
        self,
        bot: Any,
        settings: Settings,
        *,
        rate_limiter: PrefixRateLimiter | None = None,
    ) -> None:
        self.bot = bot
        self.settings = settings
        self.rate_limiter = rate_limiter or PrefixRateLimiter()
        self._warned_no_intent = False

    async def handle(self, message: discord.Message) -> bool:
        """Handle ``message`` if it is a prefix command; return whether it was."""
        # --- diagnostic logging (safe metadata only, no content) ---
        guild_id = getattr(message.guild, "id", None)
        author_id = getattr(message.author, "id", None)
        has_content = bool(getattr(message, "content", ""))
        content_len = len(getattr(message, "content", ""))
        logger.debug(
            "prefix.handle: guild=%s author=%s has_content=%s len=%s",
            guild_id, author_id, has_content, content_len,
        )

        if message.guild is None:
            logger.debug("prefix.handle: ignoring DM (no guild)")
            return False
        if getattr(message.author, "bot", False):
            logger.debug("prefix.handle: ignoring bot author=%s", author_id)
            return False
        if not getattr(message, "content", ""):
            logger.debug("prefix.handle: ignoring empty content author=%s", author_id)
            return False
        if self.bot.user is not None and message.author.id == self.bot.user.id:
            logger.debug("prefix.handle: ignoring own message author=%s", author_id)
            return False
        if not self._content_available():
            logger.debug("prefix.handle: message_content intent unavailable")
            return False

        prefix = self._prefix_for(message.guild)
        starts_with_prefix = message.content.startswith(prefix)
        logger.debug(
            "prefix.handle: guild=%s prefix=%r starts_with=%s",
            guild_id, prefix, starts_with_prefix,
        )

        parsed = parse_prefix_command(message.content, prefix)
        if parsed is None:
            logger.debug(
                "prefix.handle: parse_prefix_command returned None (guild=%s prefix=%r)",
                guild_id, prefix,
            )
            return False
        logger.debug(
            "prefix.handle: parsed name=%s sub=%s args_len=%d",
            parsed.name, parsed.subcommand, len(parsed.arguments),
        )

        if not self.rate_limiter.allow(message.author.id):
            await self._reply(
                message,
                embed=error_embed("Rate Limited", "You're sending commands too quickly — please slow down."),
            )
            return True

        logger.debug(
            "prefix command: guild=%s author=%s command=%s",
            message.guild.id,
            message.author.id,
            parsed.name,
        )
        try:
            await self._route(message, parsed)
        except ModerationError as exc:
            await self._reply(message, embed=error_embed("Error", exc.user_message))
        except Exception:  # noqa: BLE001
            logger.exception(
                "unhandled prefix command failure: guild=%s command=%s",
                message.guild.id,
                parsed.name,
            )
            await self._reply(
                message,
                embed=error_embed("Error", "Something went wrong while running that command."),
            )
        self.rate_limiter.prune()
        return True

    def _content_available(self) -> bool:
        intents = getattr(self.bot, "intents", None)
        if intents is not None and not getattr(intents, "message_content", False):
            if not self._warned_no_intent:
                logger.warning(
                    "message_content intent is disabled — prefix commands "
                    "will not work. Enable RIYXOEN_ENABLE_MESSAGE_CONTENT_INTENT=1."
                )
                self._warned_no_intent = True
            return False
        return True

    # ------------------------------------------------------------- routing

    def _prefix_for(self, guild: Any) -> str:
        config_service = getattr(self.bot, "config_service", None)
        if config_service is not None and guild is not None:
            try:
                return config_service.get(guild.id).command_prefix
            except Exception:  # noqa: BLE001
                logger.exception(
                    "could not load per-guild command prefix for guild %s",
                    getattr(guild, "id", None),
                )
        return self.settings.command_prefix

    async def _route(self, message: discord.Message, parsed: ParsedCommand) -> None:
        logger.debug(
            "prefix.route: guild=%s name=%s sub=%s",
            getattr(message.guild, "id", None), parsed.name, parsed.subcommand,
        )
        if parsed.name in ("el", "cr"):
            await self._route_custom_roles(message, parsed)
            return
        if parsed.name == "help":
            logger.debug("prefix.route: dispatching to _route_help")
            await self._route_help(message)
            return
        if parsed.name == "afk":
            logger.debug("prefix.route: dispatching to _route_afk")
            await self._route_afk(message, parsed)
            return
        if parsed.name == "jail":
            logger.debug("prefix.route: dispatching to _route_jail")
            await self._route_jail(message, parsed)
            return
        if parsed.name == "unjail":
            await self._route_unjail(message, parsed)
            return
        if parsed.name == "untimeout":
            await self._route_untimeout(message, parsed)
            return
        handler = _MODERATION_HANDLERS.get(parsed.name)
        if handler is not None:
            await handler(self, message, parsed)
            return
        prefix = self._prefix_for(message.guild)
        await self._reply(
            message,
            embed=error_embed("Unknown Command", f"Try `{prefix}help` for the command list."),
        )

    # -------------------------------------------------------- custom roles

    async def _route_custom_roles(self, message: discord.Message, parsed: ParsedCommand) -> None:
        service: CustomRoleService = self.bot.custom_roles
        subcommand = parsed.subcommand or ""
        prefix = self._prefix_for(message.guild)
        cmd = parsed.name  # "el" or "cr"

        if subcommand == "enable":
            enabled = await service.enable(message.guild, message.author)
            if not enabled:
                await self._reply(
                    message,
                    embed=info_embed("Already Enabled", "The custom-role system is already enabled in this server."),
                )
                return
            role = service.managed_role(message.guild)
            await self._reply(
                message,
                embed=custom_role_embed(
                    "Custom Role Enabled",
                    f"Managed role: {getattr(role, 'mention', 'Created')} "
                    f"({getattr(role, 'name', 'Eclipse Custom')})",
                    fields=[
                        ("Rename", f"`{prefix}{cmd} rename <name>`"),
                        ("Color", f"`{prefix}{cmd} color <hex>`"),
                    ],
                ),
            )
            return

        if subcommand == "rename":
            if not parsed.arguments.strip():
                raise InvalidTargetError(f"Usage: `{prefix}{cmd} rename <new name>` — provide a role name.")
            await service.rename(message.guild, message.author, parsed.arguments.strip())
            await self._reply(
                message,
                embed=custom_role_embed("Role Renamed", f"Custom role renamed to **{parsed.arguments.strip()}**."),
            )
            return

        if subcommand == "color":
            if not parsed.arguments.strip():
                raise InvalidTargetError(
                    f"Usage: `{prefix}{cmd} color <hex>` — provide a hex color like #ff0000."
                )
            hex_color = parsed.arguments.strip()
            await service.color(message.guild, message.author, hex_color)
            await self._reply(
                message,
                embed=custom_role_embed(
                    "Color Updated",
                    f"Custom role color set to **#{hex_color.lstrip('#').lower()}**.",
                ),
            )
            return

        raise InvalidTargetError(
            f"Unknown `{prefix}{cmd}` subcommand. Available: "
            f"`{prefix}{cmd} enable`, `{prefix}{cmd} rename <name>`, "
            f"`{prefix}{cmd} color <hex>`."
        )

    # --------------------------------------------------------------- AFK

    async def _route_afk(self, message: discord.Message, parsed: ParsedCommand) -> None:
        afk_service = getattr(self.bot, "afk_service", None)
        if afk_service is None:
            await self._reply(message, embed=error_embed("Unavailable", "The AFK system is not available."))
            return

        guild = message.guild
        author = message.author
        state = afk_service.get(guild.id, author.id)

        # If already AFK, remove AFK state
        if state is not None:
            removed = afk_service.remove(guild.id, author.id)
            if removed:
                # Restore nickname
                original_name = removed.original_name
                display = getattr(author, "display_name", author.name)
                if display.startswith("AFK | "):
                    await afk_service.restore_nickname(author, original_name)
            await self._reply(
                message,
                embed=success_embed("Welcome Back!", f"Welcome back, {author.mention}! Your AFK has been removed."),
            )
            return

        # Set AFK state
        display_name = getattr(author, "display_name", author.name)
        afk_message = parsed.arguments.strip() if parsed.arguments else ""
        afk_service.set_afk(guild.id, author.id, display_name, afk_message)

        # Change nickname
        afk_name = f"AFK | {display_name}"
        await afk_service.apply_nickname(member=author, afk_name=afk_name)

        desc = f"{author.mention} is now AFK."
        if afk_message:
            desc += f"\n> {afk_message}"
        await self._reply(message, embed=afk_embed("AFK", desc))

    # --------------------------------------------------------------- jail

    async def _route_jail(self, message: discord.Message, parsed: ParsedCommand) -> None:
        jail_service = getattr(self.bot, "jail_service", None)
        if jail_service is None:
            await self._reply(message, embed=error_embed("Unavailable", "The jail system is not available."))
            return

        subcommand = parsed.subcommand or ""
        prefix = self._prefix_for(message.guild)

        if subcommand == "setup":
            try:
                await jail_service.setup(message.guild, message.author)
                role = jail_service.get_jail_role(message.guild)
                channel = jail_service.get_jail_channel(message.guild)
                fields = []
                if role:
                    fields.append(("Jail Role", role.mention))
                if channel:
                    fields.append(("Jail Channel", channel.mention))
                else:
                    fields.append(("Jail Channel", "Not configured"))
                await self._reply(
                    message,
                    embed=jail_embed(
                        "Jail System Configured",
                        "The jail system is now active.",
                        fields=fields,
                    ),
                )
            except ModerationError as exc:
                await self._reply(message, embed=error_embed("Setup Failed", exc.user_message))
            return

        if subcommand in ("jail", ""):
            # ·jail @user [reason]
            if not parsed.subcommand:
                # It's ·jail @user [reason] (no subcommand, just target)
                pass
            else:
                # subcommand was "jail" which is wrong
                raise InvalidTargetError(
                    f"Usage: `{prefix}jail @user [reason]` or `{prefix}jail setup`."
                )

        # Jail a user
        target_id_text, reason = split_target_and_reason(parsed.arguments)
        if target_id_text is None:
            raise InvalidTargetError(f"Mention the user to jail, e.g. `{prefix}jail @user reason`.")
        member = message.guild.get_member(int(target_id_text))
        if member is None:
            raise InvalidTargetError("User not found.")

        if not reason:
            reason = "No reason provided"

        try:
            previous_role_ids = await jail_service.jail(
                message.guild, message.author, member, reason
            )
            # Create a jail case
            service: ModerationService = self.bot.moderation_service
            record = await service._punish(
                "jail",
                message.guild,
                message.author,
                member,
                reason=reason,
                execute=lambda: asyncio.sleep(0),  # type: ignore[misc]  # no-op, jail already applied
                metadata={"previous_role_ids": previous_role_ids},
            )
            await self._reply(
                message,
                embed=jail_embed(
                    "User Jailed",
                    fields=[
                        ("Target", member.mention),
                        ("Moderator", message.author.mention),
                        ("Reason", reason),
                        ("Case", f"#{record.case_id}"),
                    ],
                ),
            )
        except ModerationError as exc:
            await self._reply(message, embed=error_embed("Jail Failed", exc.user_message))

    async def _route_unjail(self, message: discord.Message, parsed: ParsedCommand) -> None:
        jail_service = getattr(self.bot, "jail_service", None)
        if jail_service is None:
            await self._reply(message, embed=error_embed("Unavailable", "The jail system is not available."))
            return

        prefix = self._prefix_for(message.guild)
        target_id_text, _reason = split_target_and_reason(parsed.arguments)
        if target_id_text is None:
            raise InvalidTargetError(f"Mention the user to unjail, e.g. `{prefix}unjail @user`.")
        member = message.guild.get_member(int(target_id_text))
        if member is None:
            raise InvalidTargetError("User not found.")

        # Find previous roles from the jail case
        case_service = self.bot.case_service
        jail_meta = jail_service.find_jail_case_metadata(
            message.guild.id, member.id, case_service
        )
        previous_role_ids = (jail_meta or {}).get("previous_role_ids", [])

        try:
            await jail_service.unjail(
                message.guild, message.author, member, previous_role_ids
            )
            # Create an unjail case
            service: ModerationService = self.bot.moderation_service
            record = await service._punish(
                "unjail",
                message.guild,
                message.author,
                member,
                reason="Released from jail",
                execute=lambda: asyncio.sleep(0),  # type: ignore[misc]
                metadata={"previous_role_ids": previous_role_ids},
            )
            await self._reply(
                message,
                embed=success_embed(
                    "User Unjailed",
                    fields=[
                        ("Target", member.mention),
                        ("Moderator", message.author.mention),
                        ("Roles Restored", str(len(previous_role_ids))),
                        ("Case", f"#{record.case_id}"),
                    ],
                ),
            )
        except ModerationError as exc:
            await self._reply(message, embed=error_embed("Unjail Failed", exc.user_message))

    # ---------------------------------------------------------- untimeout

    async def _route_untimeout(self, message: discord.Message, parsed: ParsedCommand) -> None:
        prefix = self._prefix_for(message.guild)
        target_id_text, reason = split_target_and_reason(parsed.arguments)
        if target_id_text is None:
            raise InvalidTargetError(f"Mention the user, e.g. `{prefix}untimeout @user [reason]`.")
        member = message.guild.get_member(int(target_id_text))
        if member is None:
            raise InvalidTargetError("User not found.")

        service: ModerationService = self.bot.moderation_service
        record = await service.untimeout(
            message.guild, message.author, member, reason or "No reason provided"
        )
        await self._reply(
            message,
            embed=moderation_action_embed(
                case_id=record.case_id,
                action="untimeout",
                target=member.mention,
                moderator=message.author.mention,
                reason=record.reason,
                status="Success" if record.success else "Failed",
            ),
        )

    # ---------------------------------------------------------------- help

    async def _route_help(self, message: discord.Message) -> None:
        permissions = self.bot.permissions
        guild = message.guild
        author = message.author
        prefix = self._prefix_for(guild)
        can_moderate = permissions.is_moderator(author, ACTION_WARN)
        is_admin = permissions.is_administrator(author)
        can_manage_roles = is_admin or bool(
            getattr(author.guild_permissions, "manage_roles", False)
        )

        embed = discord.Embed(
            title="Eclipse Help",
            description=f"Prefix: `{prefix}`",
            color=0x3498DB,
        )

        if can_moderate:
            mod_cmds = (
                f"`{prefix}warn @user <reason>` — Warn a member\n"
                f"`{prefix}warnings @user` — Show warnings\n"
                f"`{prefix}clearwarnings @user` — Clear warnings\n"
                f"`{prefix}modhistory @user [page]` — Moderation history\n"
                f"`{prefix}purge <amount>` — Bulk-delete messages\n"
                f"`{prefix}slowmode <seconds>` — Set slowmode\n"
                f"`{prefix}lockdown` / `{prefix}unlockdown` — Lock/unlock channel\n"
                f"`{prefix}ban @user <reason>` — Ban a member\n"
                f"`{prefix}kick @user <reason>` — Kick a member\n"
                f"`{prefix}mute @user <duration> <reason>` — Mute a member\n"
                f"`{prefix}unmute @user <reason>` — Unmute a member\n"
                f"`{prefix}untimeout @user [reason]` — Remove timeout"
            )
            embed.add_field(name="Moderation", value=mod_cmds, inline=False)

        if can_manage_roles:
            cr_cmds = (
                f"`{prefix}cr enable` — Enable custom-role system\n"
                f"`{prefix}cr rename <name>` — Rename the managed role\n"
                f"`{prefix}cr color <hex>` — Change the role color"
            )
            embed.add_field(name="Custom Roles", value=cr_cmds, inline=False)

        if is_admin:
            jail_cmds = (
                f"`{prefix}jail setup` — Configure the jail system\n"
                f"`{prefix}jail @user [reason]` — Jail a member\n"
                f"`{prefix}unjail @user` — Release from jail"
            )
            embed.add_field(name="Jail", value=jail_cmds, inline=False)
            embed.add_field(
                name="Configuration",
                value="`/config …` — Server configuration\n`/automod …` — Automated moderation",
                inline=False,
            )

        util_cmds = (
            f"`{prefix}afk [message]` — Set AFK status\n"
            f"`{prefix}help` — Show this help\n"
            "`/ping` — Bot latency\n"
            "`/case <id>` · `/cases <member>` — Case history"
        )
        embed.add_field(name="Utility", value=util_cmds, inline=False)

        if not (can_moderate or can_manage_roles or is_admin):
            embed.set_footer(text="Some command categories require moderator or administrator permissions.")

        await self._reply(message, embed=embed)

    # ------------------------------------------------------------ moderation

    async def _resolve_member(self, message: discord.Message, arguments: str):
        member_id_text, _reason = split_target_and_reason(arguments)
        if member_id_text is None:
            raise InvalidTargetError(
                "Mention the user you want to target."
            )
        member = message.guild.get_member(int(member_id_text))
        if member is None:
            raise InvalidTargetError("User not found.")
        return member

    async def _run_moderation(
        self,
        message: discord.Message,
        parsed: ParsedCommand,
        *,
        action: str,
    ) -> None:
        service: ModerationService = self.bot.moderation_service
        arguments = parsed.arguments
        reason = split_target_and_reason(arguments)[1]
        if not reason:
            raise InvalidTargetError("A reason is required for this action.")
        member = await self._resolve_member(message, arguments)

        if action == "warn":
            record = await service.warn(message.guild, message.author, member, reason)
        elif action == "ban":
            record = await service.ban(message.guild, message.author, member, reason)
        elif action == "kick":
            record = await service.kick(message.guild, message.author, member, reason)
        elif action == "mute":
            duration_text, mute_reason = _split_duration_and_reason(arguments)
            if not mute_reason:
                raise InvalidTargetError("A reason is required for this action.")
            duration_seconds = parse_duration(duration_text)
            record = await service.mute(
                message.guild, message.author, member, duration_seconds, mute_reason
            )
        elif action == "unmute":
            record = await service.unmute(message.guild, message.author, member, reason)
        else:  # pragma: no cover
            raise InvalidTargetError("Unknown command.")

        await self._reply(
            message,
            embed=moderation_action_embed(
                case_id=record.case_id,
                action=record.action,
                target=member.mention,
                moderator=message.author.mention,
                reason=record.reason,
                status="Success" if record.success else "Failed",
                duration=f"{record.duration_seconds}s" if record.duration_seconds else None,
                extra_fields=[
                    ("Note", "_The user could not be DM'd (their DMs may be closed)._")
                ] if (record.metadata or {}).get("dm_delivered") is False else None,
            ),
        )

    # ------------------------------------------------------- case queries

    async def _require_view_permissions(self, message: discord.Message, action: str) -> None:
        permissions = self.bot.permissions
        permissions.require_guild(message.guild)
        permissions.require_moderator(message.author, action)

    async def _run_warnings(self, message: discord.Message, parsed: ParsedCommand) -> None:
        await self._require_view_permissions(message, ACTION_VIEW_CASES)
        member = await self._resolve_member(message, parsed.arguments)
        case_service = self.bot.case_service
        active = case_service.count_active_warnings(message.guild.id, member.id, since=None)
        page = case_service.list_for_member(message.guild.id, member.id, page_size=10)
        warnings = [record for record in page.items if record.action == "warn"]
        if not warnings:
            await self._reply(
                message,
                embed=info_embed("No Warnings", f"{member.mention} has no warnings."),
            )
            return

        lines = []
        for record in warnings:
            status = "Cleared" if record.status == STATUS_CLEARED else "Active"
            lines.append(
                f"`#{record.case_id}` {status} | "
                f"{_truncate_reason(record.reason)} | {record.created_at:%Y-%m-%d}"
            )
        await self._reply(
            message,
            embed=info_embed(
                f"Warnings for {member.display_name}",
                f"{active} active warning(s)\n\n" + "\n".join(lines),
            ),
        )

    async def _run_clearwarnings(self, message: discord.Message, parsed: ParsedCommand) -> None:
        await self._require_view_permissions(message, ACTION_WARN)
        member = await self._resolve_member(message, parsed.arguments)
        case_service = self.bot.case_service
        cleared = 0
        page_number = 1
        while True:
            page = case_service.list_for_member(
                message.guild.id, member.id, page=page_number, page_size=50
            )
            for record in page.items:
                if record.action != "warn" or record.status != STATUS_SUCCESS:
                    continue
                updated = case_service.update_status(
                    message.guild.id, record.case_id, STATUS_CLEARED
                )
                if updated is not None:
                    cleared += 1
            if not page.has_more:
                break
            page_number += 1
        if cleared == 0:
            await self._reply(
                message,
                embed=info_embed("No Warnings", f"{member.mention} has no warnings to clear."),
            )
            return
        await self.bot.moderation_service.post_event(
            message.guild,
            title="Warnings cleared",
            fields=[
                ("Moderator", message.author.mention),
                ("Target", member.mention),
                ("Cleared", str(cleared)),
            ],
        )
        await self._reply(
            message,
            embed=success_embed(
                "Warnings Cleared",
                f"Cleared {cleared} warning(s) for {member.mention}.",
            ),
        )

    async def _run_modhistory(self, message: discord.Message, parsed: ParsedCommand) -> None:
        await self._require_view_permissions(message, ACTION_VIEW_CASES)
        rest = split_target_and_reason(parsed.arguments)[1]
        member = await self._resolve_member(message, parsed.arguments)
        page = 1
        if rest.strip().isdigit():
            page = max(int(rest.strip()), 1)
        case_service = self.bot.case_service
        result = case_service.list_for_member(message.guild.id, member.id, page=page, page_size=10)
        if not result.items:
            await self._reply(
                message,
                embed=info_embed("No Cases", f"No moderation cases found for {member.mention}."),
            )
            return
        await self._reply(
            message,
            format_case_list(
                result,
                member_label=member.mention,
                label=lambda user_id: self._member_label(message.guild, user_id),
            ),
        )

    # ----------------------------------------------------- channel commands

    async def _run_purge(self, message: discord.Message, parsed: ParsedCommand) -> None:
        service: ModerationService = self.bot.moderation_service
        parts = parsed.arguments.strip().split(maxsplit=1)
        amount_text = parts[0] if parts else ""
        if not amount_text or not amount_text.isdigit():
            raise InvalidTargetError(
                "Usage: `·purge <amount>` — provide a number of messages to delete."
            )
        amount = int(amount_text)
        max_amount = service.max_purge_amount_for(message.guild)
        validated = validate_purge_amount(amount, max_amount)
        channel = message.channel
        if not hasattr(channel, "purge"):
            raise InvalidTargetError("Purge only works in text channels.")
        record = await service.purge(channel, message.author, validated)
        await self._reply(
            message,
            embed=moderation_action_embed(
                case_id=record.case_id,
                action="purge",
                target=getattr(channel, "mention", "<channel>"),
                moderator=message.author.mention,
                reason=record.reason,
                status="Success",
            ),
        )

    async def _run_slowmode(self, message: discord.Message, parsed: ParsedCommand) -> None:
        service: ModerationService = self.bot.moderation_service
        duration_text = parsed.arguments.strip()
        if not duration_text:
            raise InvalidTargetError(
                "Usage: `·slowmode <seconds>` — e.g. `·slowmode 10` or `·slowmode 0` to clear."
            )
        cleaned = duration_text.lower()
        if cleaned in ("0", "0s"):
            seconds = 0
        else:
            seconds = validate_slowmode_duration(parse_duration(cleaned))
        channel = message.channel
        if not hasattr(channel, "edit"):
            raise InvalidTargetError("Slowmode only works in text channels.")
        record = await service.slowmode(
            message.guild,
            message.author,
            channel,
            seconds,
            self._default_reason(""),
        )
        await self._reply(
            message,
            embed=moderation_action_embed(
                case_id=record.case_id,
                action="slowmode",
                target=getattr(channel, "mention", "<channel>"),
                moderator=message.author.mention,
                reason=record.reason,
                status="Success",
            ),
        )

    async def _run_lockdown(self, message: discord.Message, parsed: ParsedCommand) -> None:
        service: ModerationService = self.bot.moderation_service
        channel = message.channel
        if not hasattr(channel, "set_permissions"):
            raise InvalidTargetError("Lockdown only works in text channels.")
        if service.is_channel_locked(message.guild, channel):
            await self._reply(
                message,
                embed=info_embed("Already Locked", "This channel is already locked."),
            )
            return
        record = await service.lock_channel(
            message.guild,
            message.author,
            channel,
            self._default_reason(parsed.arguments),
        )
        await self._reply(
            message,
            embed=moderation_action_embed(
                case_id=record.case_id,
                action="lock",
                target=getattr(channel, "mention", "<channel>"),
                moderator=message.author.mention,
                reason=record.reason,
                status="Success",
            ),
        )

    async def _run_unlockdown(self, message: discord.Message, parsed: ParsedCommand) -> None:
        service: ModerationService = self.bot.moderation_service
        channel = message.channel
        if not hasattr(channel, "set_permissions"):
            raise InvalidTargetError("Unlock only works in text channels.")
        if not service.is_channel_locked(message.guild, channel):
            await self._reply(
                message,
                embed=info_embed("Not Locked", "This channel isn't locked."),
            )
            return
        record = await service.unlock_channel(
            message.guild,
            message.author,
            channel,
            self._default_reason(parsed.arguments),
        )
        await self._reply(
            message,
            embed=moderation_action_embed(
                case_id=record.case_id,
                action="unlock",
                target=getattr(channel, "mention", "<channel>"),
                moderator=message.author.mention,
                reason=record.reason,
                status="Success",
            ),
        )

    # -------------------------------------------------------------- helpers

    @staticmethod
    def _default_reason(arguments: str) -> str:
        cleaned = arguments.strip()
        return cleaned or "No reason provided"

    def _member_label(self, guild: Any, user_id: int) -> str:
        member = guild.get_member(user_id) if getattr(guild, "get_member", None) else None
        if member is not None:
            return getattr(member, "mention", f"<@{user_id}>")
        return f"<@{user_id}>"

    async def _reply(
        self,
        message: discord.Message,
        content: str | None = None,
        embed: discord.Embed | None = None,
    ) -> None:
        try:
            kwargs: dict[str, Any] = {}
            if content:
                kwargs["content"] = content
            if embed:
                kwargs["embed"] = embed
            await message.channel.send(**kwargs)
        except Exception:  # noqa: BLE001
            logger.warning(
                "could not reply to prefix command: guild=%s channel=%s",
                message.guild.id,
                getattr(message.channel, "id", None),
            )


def _truncate_reason(reason: str | None) -> str:
    text = (reason or "—").strip()
    if len(text) <= 60:
        return text
    return text[:59] + "…"


def _split_duration_and_reason(arguments: str) -> tuple[str, str]:
    _target, rest = split_target_and_reason(arguments)
    parts = rest.split(maxsplit=1)
    if not parts:
        return "", ""
    return parts[0], parts[1] if len(parts) > 1 else ""


#: name -> handler for the quick-moderation prefix commands.
_MODERATION_HANDLERS: dict[str, Any] = {
    "ban": lambda dispatcher, message, parsed: dispatcher._run_moderation(  # noqa: SLF001
        message, parsed, action="ban"
    ),
    "kick": lambda dispatcher, message, parsed: dispatcher._run_moderation(  # noqa: SLF001
        message, parsed, action="kick"
    ),
    "mute": lambda dispatcher, message, parsed: dispatcher._run_moderation(  # noqa: SLF001
        message, parsed, action="mute"
    ),
    "unmute": lambda dispatcher, message, parsed: dispatcher._run_moderation(  # noqa: SLF001
        message, parsed, action="unmute"
    ),
    "warn": lambda dispatcher, message, parsed: dispatcher._run_moderation(  # noqa: SLF001
        message, parsed, action="warn"
    ),
    "warnings": lambda dispatcher, message, parsed: dispatcher._run_warnings(  # noqa: SLF001
        message, parsed
    ),
    "clearwarnings": lambda dispatcher, message, parsed: dispatcher._run_clearwarnings(  # noqa: SLF001
        message, parsed
    ),
    "modhistory": lambda dispatcher, message, parsed: dispatcher._run_modhistory(  # noqa: SLF001
        message, parsed
    ),
    "purge": lambda dispatcher, message, parsed: dispatcher._run_purge(  # noqa: SLF001
        message, parsed
    ),
    "slowmode": lambda dispatcher, message, parsed: dispatcher._run_slowmode(  # noqa: SLF001
        message, parsed
    ),
    "lockdown": lambda dispatcher, message, parsed: dispatcher._run_lockdown(  # noqa: SLF001
        message, parsed
    ),
    "unlockdown": lambda dispatcher, message, parsed: dispatcher._run_unlockdown(  # noqa: SLF001
        message, parsed
    ),
}
