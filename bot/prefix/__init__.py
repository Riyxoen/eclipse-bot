"""Prefix (text) commands for the bot (Phase 6 + Phase 10).

The bot's primary interface is slash commands, but a set of text commands
uses a configurable prefix (``.`` by default):

    .el enable | .el rename <name> | .el color <hex>
    .warn @user <reason> | .warnings @user | .clearwarnings @user
    .modhistory @user [page]
    .ban @user <reason> | .kick @user <reason>
    .mute @user <duration> <reason> | .unmute @user <reason>
    .purge <amount> | .slowmode <seconds>
    .lockdown | .unlockdown
    .help

The prefix is per-guild: an administrator can change it with ``/config
prefix`` and the dispatcher honors it per message (Phase 10).

Handlers stay thin: they parse the message, delegate to the service layer
(:class:`bot.services.custom_roles.CustomRoleService` and
:class:`bot.services.moderation.ModerationService`), and format safe
responses. All permission, hierarchy, and state checks live in the services
— never here.

Safety:

- Prefix commands require the ``message_content`` intent (on by default
  since Phase 6); when it is unavailable the dispatcher logs once and no-ops.
- Only guild messages are processed; bots are ignored.
- Every failure is caught and surfaced as a safe message; details go to the
  logs. Nothing internal ever reaches Discord.
- Moderation actions reuse the existing case/audit/log-channel pipeline —
  there is no second moderation system.
"""

from __future__ import annotations

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
from bot.moderation.errors import InvalidTargetError, ModerationError
from bot.moderation.response import format_case_list, format_case_response
from bot.moderation.validation import (
    parse_duration,
    validate_purge_amount,
    validate_slowmode_duration,
)
from bot.services.custom_roles import CustomRoleService
from bot.services.moderation import ModerationService

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
    """Bounded in-memory rate limiter for prefix commands (anti-abuse).

    Prevents a single user from hammering the text commands (``.ban``,
    ``.el``, ...) faster than :data:`PREFIX_COMMAND_LIMIT` commands per
    :data:`PREFIX_COMMAND_WINDOW_SECONDS` — every call past the limit is a
    Discord API request or a case write, so the limiter keeps abuse local and
    free. Memory is bounded: at most :data:`PREFIX_MAX_TRACKED_USERS` users
    are tracked, each holding a fixed-size deque of timestamps, and stale
    entries are pruned on access and on overflow (oldest-inserted first).
    """

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
        # Drop timestamps outside the window.
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
    message is not a prefix command.

    Pure and unit-testable: ``".el rename Cool"`` -> ``name="el"``,
    ``subcommand="rename"``, ``arguments="Cool"``; ``".ban @u reason"`` ->
    ``name="ban"``, ``subcommand=None``, ``arguments="@u reason"``.
    """
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
    if name == "el" and parts:
        subcommand = parts[0].lower()
        arguments = parts[1] if len(parts) > 1 else ""
        return ParsedCommand(name=name, subcommand=subcommand, arguments=arguments)
    return ParsedCommand(name=name, subcommand=None, arguments=remainder)


def parse_member_id(arguments: str) -> int | None:
    """Extract the leading user-mention ID from ``arguments``, or ``None``."""
    match = _MENTION_PATTERN.match(arguments.strip())
    if match is None:
        return None
    return int(match.group(1))


def split_target_and_reason(arguments: str) -> tuple[str | None, str]:
    """Split ``arguments`` into ``(member_id_text, reason)``.

    The target is the leading mention; the remainder is the reason (which may
    contain spaces). A missing mention yields ``(None, whole_text)`` so the
    command layer can report ``User not found.``.
    """
    stripped = arguments.strip()
    match = _MENTION_PATTERN.match(stripped)
    if match is None:
        return None, stripped
    reason = stripped[match.end() :].strip()
    return match.group(1), reason


def format_moderation_reply(record, *, target_label: str, moderator_label: str) -> str:
    """The case summary for prefix moderation commands.

    Appends a private note when the DM to the punished user could not be
    delivered (the action itself still succeeded — never the reverse).
    """
    text = format_case_response(record, target_label=target_label, moderator_label=moderator_label)
    if (record.metadata or {}).get("dm_delivered") is False:
        text += "\n_Note: the user could not be DM'd (their DMs may be closed)._"
    return text


class PrefixDispatcher:
    """Routes guild text messages starting with the configured prefix.

    ``bot`` must expose ``settings``, ``custom_roles``, ``moderation_service``,
    and ``permissions`` (set up in ``setup_hook``).
    """

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
        """Handle ``message`` if it is a prefix command; return whether it was.

        Never raises: every failure is logged and surfaced as a safe reply.
        """
        if message.guild is None:
            return False
        if getattr(message.author, "bot", False):
            return False
        if not getattr(message, "content", ""):
            return False
        if self.bot.user is not None and message.author.id == self.bot.user.id:
            return False
        if not self._content_available():
            return False

        parsed = parse_prefix_command(message.content, self._prefix_for(message.guild))
        if parsed is None:
            return False

        # Anti-abuse: a user exceeding the local command rate gets a safe
        # reply and the command is not run (no Discord API call is made).
        if not self.rate_limiter.allow(message.author.id):
            await self._reply(
                message,
                "You're sending commands too quickly — please slow down.",
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
            await self._reply(message, exc.user_message)
        except Exception:  # noqa: BLE001 - the bot must never crash on a message
            logger.exception(
                "unhandled prefix command failure: guild=%s command=%s",
                message.guild.id,
                parsed.name,
            )
            await self._reply(message, "Something went wrong while running that command.")
        self.rate_limiter.prune()
        return True

    def _content_available(self) -> bool:
        """Whether the message-content intent is available for prefix parsing."""
        intents = getattr(self.bot, "intents", None)
        if intents is not None and not getattr(intents, "message_content", False):
            if not self._warned_no_intent:
                logger.warning(
                    "message_content intent is disabled — prefix commands "
                    "(.el, .ban, ...) will not work. Enable "
                    "RIYXOEN_ENABLE_MESSAGE_CONTENT_INTENT=1 (and the intent "
                    "in the Discord developer portal)."
                )
                self._warned_no_intent = True
            return False
        return True

    # ------------------------------------------------------------- routing

    def _prefix_for(self, guild: Any) -> str:
        """The text-command prefix for ``guild`` (per-guild override wins).

        Phase 10: administrators set a per-guild prefix via ``/config prefix``;
        it is honored per message so the change applies immediately. A config
        read failure degrades to the global environment prefix.
        """
        config_service = getattr(self.bot, "config_service", None)
        if config_service is not None and guild is not None:
            try:
                return config_service.get(guild.id).command_prefix
            except Exception:  # noqa: BLE001 - config must never break commands
                logger.exception(
                    "could not load per-guild command prefix for guild %s",
                    getattr(guild, "id", None),
                )
        return self.settings.command_prefix

    async def _route(self, message: discord.Message, parsed: ParsedCommand) -> None:
        if parsed.name == "el":
            await self._route_el(message, parsed)
            return
        if parsed.name == "help":
            await self._route_help(message)
            return
        handler = _MODERATION_HANDLERS.get(parsed.name)
        if handler is not None:
            await handler(self, message, parsed)
            return
        # The message started with the prefix and a command word, but there is
        # no handler for it. Reply clearly instead of silently ignoring it.
        await self._reply(message, "Command unavailable. Try `.help` for the command list.")

    async def _route_el(self, message: discord.Message, parsed: ParsedCommand) -> None:
        service: CustomRoleService = self.bot.custom_roles
        subcommand = parsed.subcommand or ""
        if subcommand == "enable":
            enabled = await service.enable(message.guild, message.author)
            if not enabled:
                # Idempotent: already enabled before this call.
                await self._reply(
                    message,
                    "The custom-role system is already enabled in this server.",
                )
                return
            role = service.managed_role(message.guild)
            await self._reply(
                message,
                "Custom-role system enabled. "
                f"Managed role: **{getattr(role, 'name', 'Riyxoen Custom')}** "
                f"({getattr(role, 'mention', '')}). Use `.el rename <name>` and "
                "`.el color <hex>` to customize it.",
            )
            return
        if subcommand == "rename":
            if not parsed.arguments.strip():
                raise InvalidTargetError("Usage: `.el rename <new name>` — provide a role name.")
            await service.rename(message.guild, message.author, parsed.arguments.strip())
            await self._reply(message, f"Custom role renamed to **{parsed.arguments.strip()}**.")
            return
        if subcommand == "color":
            if not parsed.arguments.strip():
                raise InvalidTargetError(
                    "Usage: `.el color <hex>` — provide a hex color like #ff0000."
                )
            hex_color = parsed.arguments.strip()
            await service.color(message.guild, message.author, hex_color)
            await self._reply(
                message,
                f"Custom role color set to **#{hex_color.lstrip('#').lower()}**.",
            )
            return
        raise InvalidTargetError(
            "Unknown `.el` subcommand. Available: `.el enable`, `.el rename <name>`, "
            "`.el color <hex>`."
        )

    # ---------------------------------------------------------------- help

    async def _route_help(self, message: discord.Message) -> None:
        """Show organized command help; restricted sections are hidden for
        users who cannot use them (Phase 10)."""
        permissions = self.bot.permissions
        guild = message.guild
        author = message.author
        prefix = self._prefix_for(guild)
        can_moderate = permissions.is_moderator(author, ACTION_WARN)
        is_admin = permissions.is_administrator(author)
        can_manage_roles = is_admin or bool(
            getattr(author.guild_permissions, "manage_roles", False)
        )

        lines = [f"**Riyxoen help** — prefix `{prefix}`"]
        if can_moderate:
            lines.append("\n__Moderation__")
            lines.append(f"`{prefix}warn @user <reason>` — Warn a member")
            lines.append(f"`{prefix}warnings @user` — Show a member's warnings")
            lines.append(f"`{prefix}clearwarnings @user` — Clear a member's warnings")
            lines.append(f"`{prefix}modhistory @user [page]` — Show moderation history")
            lines.append(f"`{prefix}purge <amount>` — Bulk-delete recent messages")
            lines.append(f"`{prefix}slowmode <seconds>` — Set this channel's slowmode")
            lines.append(f"`{prefix}lockdown` / `{prefix}unlockdown` — Lock/unlock this channel")
            lines.append(f"`{prefix}ban @user <reason>` — Ban a member (DM first)")
            lines.append(f"`{prefix}kick @user <reason>` — Kick a member")
            lines.append(f"`{prefix}mute @user <duration> <reason>` — Mute a member")
            lines.append(f"`{prefix}unmute @user <reason>` — Unmute a member")
        if can_manage_roles:
            lines.append("\n__Custom Roles__")
            lines.append(f"`{prefix}el enable` — Enable the custom-role system")
            lines.append(f"`{prefix}el rename <name>` — Rename the managed role")
            lines.append(f"`{prefix}el color <hex>` — Change the role color")
        if is_admin:
            lines.append("\n__Configuration__")
            lines.append("`/config …` — Server configuration (includes the prefix)")
            lines.append("`/automod …` — Automated moderation")
        lines.append("\n__Utility__")
        lines.append(f"`{prefix}help` — Show this help")
        lines.append("`/ping` — Bot latency")
        lines.append("`/case <id>` · `/cases <member>` — Moderation history (slash)")
        if not (can_moderate or can_manage_roles or is_admin):
            lines.append(
                "\n_Some command categories require moderator or administrator permissions._"
            )
        await self._reply(message, "\n".join(lines))

    # ------------------------------------------------------------ moderation

    async def _resolve_member(self, message: discord.Message, arguments: str):
        """Resolve the target member from a mention, or raise a safe error."""
        member_id_text, _reason = split_target_and_reason(arguments)
        if member_id_text is None:
            raise InvalidTargetError(
                "Mention the user you want to target, e.g. `.ban @user reason`."
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
        else:  # pragma: no cover - routing guarantees a known action
            raise InvalidTargetError("Unknown command.")

        await self._reply(
            message,
            format_moderation_reply(
                record,
                target_label=member.mention,
                moderator_label=message.author.mention,
            ),
        )

    # ------------------------------------------------------- case queries

    async def _require_view_permissions(self, message: discord.Message, action: str) -> None:
        """Guild + moderator gate for case queries (server-side, shared)."""
        permissions = self.bot.permissions
        permissions.require_guild(message.guild)
        permissions.require_moderator(message.author, action)

    async def _run_warnings(self, message: discord.Message, parsed: ParsedCommand) -> None:
        """Show a member's warnings (active count + recent warning cases)."""
        await self._require_view_permissions(message, ACTION_VIEW_CASES)
        member = await self._resolve_member(message, parsed.arguments)
        case_service = self.bot.case_service
        active = case_service.count_active_warnings(message.guild.id, member.id, since=None)
        page = case_service.list_for_member(message.guild.id, member.id, page_size=10)
        warnings = [record for record in page.items if record.action == "warn"]
        if not warnings:
            await self._reply(message, f"{member.mention} has no warnings.")
            return
        lines = [
            f"**Warnings for {member.mention}** — {active} active",
        ]
        for record in warnings:
            lines.append(
                f"`#{record.case_id}` {_warning_status_label(record)} | "
                f"{self._member_label(message.guild, record.moderator_user_id)} | "
                f"{_truncate_reason(record.reason)} | {record.created_at:%Y-%m-%d}"
            )
        await self._reply(message, "\n".join(lines))

    async def _run_clearwarnings(self, message: discord.Message, parsed: ParsedCommand) -> None:
        """Mark every active warning of a member as cleared (history kept)."""
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
            await self._reply(message, f"{member.mention} has no warnings to clear.")
            return
        # Structured moderation log (auditable), best-effort like all posts.
        await self.bot.moderation_service.post_event(
            message.guild,
            title="Warnings cleared",
            fields=[
                ("Moderator", message.author.mention),
                ("Target", member.mention),
                ("Cleared", str(cleared)),
            ],
        )
        await self._reply(message, f"Cleared {cleared} warning(s) for {member.mention}.")

    async def _run_modhistory(self, message: discord.Message, parsed: ParsedCommand) -> None:
        """Show a member's recent moderation cases (paginated)."""
        await self._require_view_permissions(message, ACTION_VIEW_CASES)
        rest = split_target_and_reason(parsed.arguments)[1]
        member = await self._resolve_member(message, parsed.arguments)
        page = 1
        if rest.strip().isdigit():
            page = max(int(rest.strip()), 1)
        case_service = self.bot.case_service
        result = case_service.list_for_member(message.guild.id, member.id, page=page, page_size=10)
        if not result.items:
            await self._reply(message, f"No moderation cases found for {member.mention}.")
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
        """Bulk-delete up to ``amount`` messages in the current channel."""
        service: ModerationService = self.bot.moderation_service
        parts = parsed.arguments.strip().split(maxsplit=1)
        amount_text = parts[0] if parts else ""
        if not amount_text or not amount_text.isdigit():
            raise InvalidTargetError(
                "Usage: `.purge <amount>` — provide a number of messages to delete."
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
            format_moderation_reply(
                record,
                target_label=getattr(channel, "mention", "<channel>"),
                moderator_label=message.author.mention,
            ),
        )

    async def _run_slowmode(self, message: discord.Message, parsed: ParsedCommand) -> None:
        """Set the current channel's slowmode (0 clears it)."""
        service: ModerationService = self.bot.moderation_service
        duration_text = parsed.arguments.strip()
        if not duration_text:
            raise InvalidTargetError(
                "Usage: `.slowmode <seconds>` — e.g. `.slowmode 10`, `.slowmode 5m`, "
                "or `.slowmode 0` to clear."
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
            format_moderation_reply(
                record,
                target_label=getattr(channel, "mention", "<channel>"),
                moderator_label=message.author.mention,
            ),
        )

    async def _run_lockdown(self, message: discord.Message, parsed: ParsedCommand) -> None:
        """Lock the current channel (idempotent; reversible via unlockdown)."""
        service: ModerationService = self.bot.moderation_service
        channel = message.channel
        if not hasattr(channel, "set_permissions"):
            raise InvalidTargetError("Lockdown only works in text channels.")
        if service.is_channel_locked(message.guild, channel):
            await self._reply(message, "This channel is already locked.")
            return
        record = await service.lock_channel(
            message.guild,
            message.author,
            channel,
            self._default_reason(parsed.arguments),
        )
        await self._reply(
            message,
            format_moderation_reply(
                record,
                target_label=getattr(channel, "mention", "<channel>"),
                moderator_label=message.author.mention,
            ),
        )

    async def _run_unlockdown(self, message: discord.Message, parsed: ParsedCommand) -> None:
        """Unlock the current channel, restoring its pre-lock state."""
        service: ModerationService = self.bot.moderation_service
        channel = message.channel
        if not hasattr(channel, "set_permissions"):
            raise InvalidTargetError("Unlock only works in text channels.")
        if not service.is_channel_locked(message.guild, channel):
            await self._reply(message, "This channel isn't locked.")
            return
        record = await service.unlock_channel(
            message.guild,
            message.author,
            channel,
            self._default_reason(parsed.arguments),
        )
        await self._reply(
            message,
            format_moderation_reply(
                record,
                target_label=getattr(channel, "mention", "<channel>"),
                moderator_label=message.author.mention,
            ),
        )

    # -------------------------------------------------------------- helpers

    @staticmethod
    def _default_reason(arguments: str) -> str:
        """Documented default reason when a command allows omitting one."""
        cleaned = arguments.strip()
        return cleaned or "No reason provided"

    def _member_label(self, guild: Any, user_id: int) -> str:
        """Best-effort mention for a user ID (never a stored username)."""
        member = guild.get_member(user_id) if getattr(guild, "get_member", None) else None
        if member is not None:
            return getattr(member, "mention", f"<@{user_id}>")
        return f"<@{user_id}>"

    async def _reply(self, message: discord.Message, content: str) -> None:
        try:
            await message.channel.send(content)
        except Exception:  # noqa: BLE001 - replying must never crash the bot
            logger.warning(
                "could not reply to prefix command: guild=%s channel=%s",
                message.guild.id,
                getattr(message.channel, "id", None),
            )


def _warning_status_label(record: Any) -> str:
    """Short status label for a warning case (Success/Cleared/Failed)."""
    if record.status == STATUS_CLEARED:
        return "Cleared"
    return "Success" if record.success else "Failed"


def _truncate_reason(reason: str | None) -> str:
    """Truncate a reason for list views so messages stay bounded."""
    text = (reason or "—").strip()
    if len(text) <= 60:
        return text
    return text[:59] + "…"


def _split_duration_and_reason(arguments: str) -> tuple[str, str]:
    """Split ``arguments`` into ``(duration_text, reason)`` after the mention."""
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
