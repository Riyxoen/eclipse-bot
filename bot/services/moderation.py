"""Central moderation service.

Commands stay thin and delegate here. The service owns the full pipeline:

    permission checks -> Discord API -> case service -> SQLite repository

Every attempt that reaches the service produces exactly one case record
(``status`` reflects the outcome), a structured audit log line, and — for
punishments — a best-effort DM notification that can never fail the action.

Persistence-failure contract (documented decision):

    moderation action -> case persistence -> response

The case is persisted **after** the Discord mutation succeeds. If persistence
fails at that point, the action is *not* rolled back (Discord has no rollback
for kick/ban/timeout); the user is told explicitly that the action completed
but the case record could not be saved (:class:`CasePersistenceError`), and
full diagnostics go to the logs. A failed moderation action is recorded as a
``failed`` case; if *that* persistence fails, the original safe error is
still raised and the failure is logged. Exactly one case is created per
attempt — retries are separate attempts and get separate cases, never
duplicates of the original.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import timedelta
from typing import Any

import discord

from bot.configuration.settings import Settings
from bot.moderation.actions import (
    ACTION_BAN,
    ACTION_DELETE,
    ACTION_KICK,
    ACTION_LOCK,
    ACTION_PURGE,
    ACTION_SLOWMODE,
    ACTION_TIMEOUT,
    ACTION_UNBAN,
    ACTION_UNLOCK,
    ACTION_UNMUTE,
    ACTION_WARN,
    PUNISHMENT_ACTIONS,
)
from bot.moderation.cases import STATUS_FAILED, STATUS_SUCCESS, CaseRecord, utc_now
from bot.moderation.errors import (
    CasePersistenceError,
    InvalidTargetError,
    ModerationError,
    ModerationExecutionError,
    NotTimedOutError,
)
from bot.moderation.validation import validate_reason, validate_slowmode_duration
from bot.permissions.checks import PermissionChecker
from bot.services.cases import CaseService
from bot.services.notifications import NotificationService

logger = logging.getLogger("riyxoen.moderation")


class ModerationService:
    """Executes moderation actions with full validation, case records, and audit."""

    def __init__(
        self,
        bot: discord.Client,
        case_service: CaseService,
        *,
        settings: Settings,
        permissions: PermissionChecker | None = None,
        notifier: NotificationService | None = None,
        config_service: Any = None,
    ) -> None:
        self.bot = bot
        self.case_service = case_service
        self.settings = settings
        self.permissions = permissions or PermissionChecker(bot, settings)
        self.notifier = notifier or NotificationService(bot, enabled=settings.notify_users)
        #: Phase 5: when present, per-guild configuration (log channel,
        #: mod-log toggle, DM notifications, max purge) is authoritative.
        self.config_service = config_service

    # ------------------------------------------------------------ public API

    async def warn(
        self,
        guild: discord.Guild,
        moderator: discord.Member,
        target: discord.Member,
        reason: str,
        *,
        automated: bool = False,
        detector: str | None = None,
    ) -> CaseRecord:
        """Warn a member (bot-side record; no Discord mutation)."""

        async def _execute() -> None:
            return None

        return await self._punish(
            ACTION_WARN,
            guild,
            moderator,
            target,
            reason=reason,
            execute=_execute,
            automated=automated,
            detector=detector,
        )

    async def timeout(
        self,
        guild: discord.Guild,
        moderator: discord.Member,
        target: discord.Member,
        duration_seconds: int,
        reason: str,
        *,
        automated: bool = False,
        detector: str | None = None,
        muted: bool = False,
    ) -> CaseRecord:
        """Time a member out for ``duration_seconds`` (1s..28d)."""

        async def _execute() -> None:
            await target.timeout(timedelta(seconds=duration_seconds), reason=reason)

        return await self._punish(
            ACTION_TIMEOUT,
            guild,
            moderator,
            target,
            reason=reason,
            duration_seconds=duration_seconds,
            execute=_execute,
            automated=automated,
            detector=detector,
            metadata={"muted": True} if muted else None,
        )

    async def kick(
        self, guild: discord.Guild, moderator: discord.Member, target: discord.Member, reason: str
    ) -> CaseRecord:
        """Kick a member from the guild."""

        async def _execute() -> None:
            await guild.kick(target, reason=reason)

        return await self._punish(
            ACTION_KICK, guild, moderator, target, reason=reason, execute=_execute
        )

    async def ban(
        self, guild: discord.Guild, moderator: discord.Member, target: discord.Member, reason: str
    ) -> CaseRecord:
        """Ban a member (their recent messages are not deleted)."""

        async def _execute() -> None:
            await guild.ban(target, reason=reason, delete_message_seconds=0)

        return await self._punish(
            ACTION_BAN, guild, moderator, target, reason=reason, execute=_execute
        )

    async def unban(
        self, guild: discord.Guild, moderator: discord.Member, user_id: int, reason: str
    ) -> CaseRecord:
        """Unban a user by ID. The user does not need to be in the guild."""
        self.permissions.require_guild(guild)
        record = CaseRecord(
            guild_id=guild.id,
            target_user_id=user_id,
            moderator_user_id=getattr(moderator, "id", 0),
            action=ACTION_UNBAN,
            reason=reason or "",
            created_at=utc_now(),
            status=STATUS_FAILED,
        )
        try:
            cleaned = validate_reason(reason)
            self.permissions.require_moderator(moderator, ACTION_UNBAN)
            await self.permissions.require_bot_permissions(guild, ACTION_UNBAN)
            user = await self._fetch_user(user_id)
        except ModerationError as exc:
            self._record_failure(record, exc)
            raise
        record = replace(record, reason=cleaned)
        try:
            await guild.unban(user, reason=cleaned)
        except discord.NotFound:
            exc = InvalidTargetError("That user isn't banned in this server.")
            self._record_failure(record, exc)
            raise exc from None
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.info(
                "discord rejected action=%s guild=%s target=%s error=%s",
                ACTION_UNBAN,
                guild.id,
                user_id,
                type(exc).__name__,
            )
            self._record_failure(record, ModerationExecutionError())
            raise ModerationExecutionError() from None
        return await self._record_success(record, target=user, guild=guild, moderator=moderator)

    # ------------------------------------------------------------ mute family

    async def mute(
        self,
        guild: discord.Guild,
        moderator: discord.Member,
        target: discord.Member,
        duration_seconds: int,
        reason: str,
    ) -> CaseRecord:
        """Mute (time out) a member for ``duration_seconds`` (1s..28d).

        Same pipeline as :meth:`timeout` — Discord's timeout is the mute
        mechanism. The case stores ``duration_seconds`` and ``expires_at``
        (UTC) for audit and restart-safe state; Discord removes the timeout
        automatically when it expires (no background loop needed).
        """
        return await self.timeout(guild, moderator, target, duration_seconds, reason, muted=True)

    async def unmute(
        self,
        guild: discord.Guild,
        moderator: discord.Member,
        target: discord.Member,
        reason: str,
    ) -> CaseRecord:
        """Remove a member's timeout (``.unmute``).

        Refuses (safe error, no case) when the target is not currently timed
        out. Uses the member's live timeout state as the source of truth, so
        it stays correct across bot restarts and Discord-side expiry.
        """

        async def _execute() -> None:
            await target.timeout(None, reason=reason)

        return await self._punish(
            ACTION_UNMUTE,
            guild,
            moderator,
            target,
            reason=reason,
            execute=_execute,
            state_check=self._require_timed_out(target),
        )

    @staticmethod
    def _require_timed_out(target: discord.Member) -> Callable[[], None]:
        """State check for unmute: the target must currently be timed out.

        Uses the member's live timeout state (the source of truth). A
        ``timed_out_until`` in the past means Discord already lifted the
        timeout — the user is no longer muted, so unmute refuses cleanly.
        This keeps unmute correct across bot restarts.
        """

        def _check() -> None:
            until = getattr(target, "timed_out_until", None)
            if until is None or until <= utc_now():
                raise NotTimedOutError()

        return _check

    async def untimeout(
        self,
        guild: discord.Guild,
        moderator: discord.Member,
        target: discord.Member,
        reason: str,
    ) -> CaseRecord:
        """Remove a member's timeout (untimeout prefix command).

        Uses the same pipeline as unmute but creates an untimeout case
        for clarity. Refuses when the target is not currently timed out.
        """

        async def _execute() -> None:
            await target.timeout(None, reason=reason)

        return await self._punish(
            ACTION_UNMUTE,  # uses unmute action for case consistency
            guild,
            moderator,
            target,
            reason=reason,
            execute=_execute,
            state_check=self._require_timed_out(target),
        )

    async def delete_message(
        self,
        guild: discord.Guild,
        moderator: discord.Member,
        message: Any,
        reason: str,
        *,
        automated: bool = True,
        detector: str | None = None,
    ) -> CaseRecord | None:
        """Delete a single message and record a case for it.

        Used by the automated moderation engine (and future manual single-
        message deletion). ``message`` needs ``id``, ``author``, ``channel``,
        and an async ``delete()``. Returns ``None`` when the message was
        already deleted (nothing to do — no phantom case is created).
        """
        self.permissions.require_guild(guild)
        record = CaseRecord(
            guild_id=guild.id,
            target_user_id=getattr(getattr(message, "author", None), "id", 0),
            moderator_user_id=getattr(moderator, "id", 0),
            action=ACTION_DELETE,
            reason=reason or "",
            created_at=utc_now(),
            status=STATUS_FAILED,
            automated=automated,
            detector=detector,
        )
        try:
            cleaned = validate_reason(reason)
            if not automated:
                self.permissions.require_moderator(moderator, ACTION_DELETE)
            await self.permissions.require_bot_permissions(
                guild, ACTION_DELETE, channel=getattr(message, "channel", None)
            )
        except ModerationError as exc:
            self._record_failure(record, exc)
            raise
        record = replace(record, reason=cleaned)
        try:
            await message.delete()
        except discord.NotFound:
            # Already deleted (e.g. a purge or another enforcement won the
            # race). The message is gone — there is nothing to enforce and no
            # duplicate case to create. Log and return without persisting.
            logger.info(
                "message already deleted before enforcement: action=%s guild=%s message=%s",
                ACTION_DELETE,
                guild.id,
                getattr(message, "id", None),
            )
            return None
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.info(
                "discord rejected action=%s guild=%s message=%s error=%s",
                ACTION_DELETE,
                guild.id,
                getattr(message, "id", None),
                type(exc).__name__,
            )
            self._record_failure(record, ModerationExecutionError())
            raise ModerationExecutionError() from None
        return await self._record_success(
            record, target=getattr(message, "author", None), guild=guild, moderator=moderator
        )

    async def purge(
        self, channel: discord.TextChannel, moderator: discord.Member, amount: int
    ) -> CaseRecord:
        """Bulk-delete up to ``amount`` messages in ``channel`` (Discord's 14-day
        bulk-delete limit is handled internally by ``TextChannel.purge``)."""
        guild = channel.guild
        self.permissions.require_guild(guild)
        record = CaseRecord(
            guild_id=guild.id,
            target_user_id=getattr(channel, "id", 0),
            moderator_user_id=getattr(moderator, "id", 0),
            action=ACTION_PURGE,
            reason="",
            created_at=utc_now(),
            status=STATUS_FAILED,
        )
        try:
            self.permissions.require_state(ACTION_PURGE, purge_amount=amount, guild=guild)
            self.permissions.require_moderator(moderator, ACTION_PURGE)
            await self.permissions.require_bot_permissions(guild, ACTION_PURGE, channel=channel)
        except ModerationError as exc:
            self._record_failure(record, exc)
            raise
        try:
            deleted = await channel.purge(limit=amount)
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.info(
                "discord rejected action=%s guild=%s channel=%s error=%s",
                ACTION_PURGE,
                guild.id,
                getattr(channel, "id", None),
                type(exc).__name__,
            )
            self._record_failure(record, ModerationExecutionError())
            raise ModerationExecutionError() from None
        count = len(deleted)
        # Report the result accurately: if some messages could not be deleted
        # (e.g. older than Discord's 14-day bulk-delete window, or deleted in
        # the meantime), never claim they were.
        if count < amount:
            reason = (
                f"Purged {count} of {amount} messages "
                "(some could not be deleted — they may be older than 14 days)"
            )
        else:
            reason = f"Purged {count} messages"
        record = replace(record, reason=reason)
        return await self._record_success(record, target=channel, guild=guild, moderator=moderator)

    # ------------------------------------------------------ channel actions

    async def slowmode(
        self,
        guild: discord.Guild,
        moderator: discord.Member,
        channel: discord.TextChannel,
        duration_seconds: int,
        reason: str,
    ) -> CaseRecord:
        """Set a channel's slowmode delay (0..6 hours; 0 clears it)."""

        async def _execute() -> None:
            await channel.edit(slowmode_delay=duration_seconds)

        return await self._channel_action(
            ACTION_SLOWMODE,
            guild,
            moderator,
            channel,
            reason=reason,
            execute=_execute,
            duration_seconds=duration_seconds,
            state_check=lambda: validate_slowmode_duration(duration_seconds),
        )

    async def lock_channel(
        self,
        guild: discord.Guild,
        moderator: discord.Member,
        channel: discord.TextChannel,
        reason: str,
    ) -> CaseRecord:
        """Lock a channel by denying ``send_messages`` for ``@everyone``.

        The pre-lock overwrite state is recorded on the case (metadata) so
        :meth:`unlock_channel` can restore it exactly — the bot never blindly
        overwrites existing permission configuration.
        """
        default_role = guild.default_role
        previous = self._read_send_messages_overwrite(channel, default_role)
        metadata = {
            "previous_send_messages": previous,
            "had_overwrite": default_role in getattr(channel, "overwrites", {}),
        }

        async def _execute() -> None:
            await channel.set_permissions(default_role, send_messages=False, reason=reason)

        return await self._channel_action(
            ACTION_LOCK,
            guild,
            moderator,
            channel,
            reason=reason,
            execute=_execute,
            metadata=metadata,
        )

    async def unlock_channel(
        self,
        guild: discord.Guild,
        moderator: discord.Member,
        channel: discord.TextChannel,
        reason: str,
    ) -> CaseRecord:
        """Unlock a channel, restoring the exact pre-lock permission state.

        The restore state comes from the most recent ``lock`` case for this
        channel (recorded in its metadata). Without a matching lock case the
        action is refused with a safe error.
        """
        lock_case = self._find_lock_case(guild.id, channel.id)
        if lock_case is None:
            raise InvalidTargetError("This channel isn't locked.")
        metadata = lock_case.metadata or {}
        previous = metadata.get("previous_send_messages")
        had_overwrite = bool(metadata.get("had_overwrite", False))
        default_role = guild.default_role

        async def _execute() -> None:
            if not had_overwrite:
                # The lock created the @everyone overwrite: removing it is the
                # exact inverse of the lock.
                await channel.set_permissions(default_role, overwrite=None, reason=reason)
            else:
                # Restore the previous value (True/False/None=inherited).
                await channel.set_permissions(default_role, send_messages=previous, reason=reason)

        return await self._channel_action(
            ACTION_UNLOCK,
            guild,
            moderator,
            channel,
            reason=reason,
            execute=_execute,
        )

    async def _channel_action(
        self,
        action: str,
        guild: discord.Guild,
        moderator: discord.Member,
        channel: discord.TextChannel,
        *,
        reason: str,
        execute: Callable[[], Awaitable[Any]],
        duration_seconds: int | None = None,
        state_check: Callable[[], Any] | None = None,
        metadata: dict | None = None,
    ) -> CaseRecord:
        """Shared pipeline for channel-scoped actions (slowmode/lock/unlock)."""
        self.permissions.require_guild(guild)
        record = CaseRecord(
            guild_id=guild.id,
            target_user_id=getattr(channel, "id", 0),
            moderator_user_id=getattr(moderator, "id", 0),
            action=action,
            reason=reason or "",
            created_at=utc_now(),
            status=STATUS_FAILED,
            duration_seconds=duration_seconds,
            metadata=metadata,
        )
        try:
            cleaned = validate_reason(reason)
            if state_check is not None:
                state_check()
            self.permissions.require_moderator(moderator, action)
            await self.permissions.require_bot_permissions(guild, action, channel=channel)
        except ModerationError as exc:
            self._record_failure(record, exc)
            raise
        record = replace(record, reason=cleaned)
        try:
            await execute()
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.info(
                "discord rejected action=%s guild=%s channel=%s error=%s",
                action,
                guild.id,
                getattr(channel, "id", None),
                type(exc).__name__,
            )
            self._record_failure(record, ModerationExecutionError())
            raise ModerationExecutionError() from None
        return await self._record_success(record, target=channel, guild=guild, moderator=moderator)

    @staticmethod
    def _read_send_messages_overwrite(channel: Any, target: Any) -> bool | None:
        """Read the current ``send_messages`` value of an overwrite (None=unset)."""
        try:
            overwrite = channel.overwrites_for(target)
        except AttributeError:
            return None
        return getattr(overwrite, "send_messages", None)

    def is_channel_locked(self, guild: discord.Guild, channel: discord.TextChannel) -> bool:
        """Whether ``channel``'s @everyone overwrite currently denies sending.

        Read-only state check used by the ``.lockdown``/``.unlockdown`` prefix
        commands (Phase 10) so repeated calls are idempotent and never create
        duplicate lock cases. Uses the live overwrite, not memory.
        """
        return self._read_send_messages_overwrite(channel, guild.default_role) is False

    def _find_lock_case(self, guild_id: int, channel_id: int) -> CaseRecord | None:
        """The most recent successful ``lock`` case for ``channel_id``."""
        page = self.case_service.list_for_guild(guild_id, page_size=50)
        for record in page.items:  # newest first
            if record.action == ACTION_LOCK and record.target_user_id == channel_id:
                return record
        return None

    # -------------------------------------------------------------- pipeline

    async def _punish(
        self,
        action: str,
        guild: discord.Guild,
        moderator: discord.Member,
        target: discord.Member,
        *,
        reason: str,
        duration_seconds: int | None = None,
        execute: Callable[[], Awaitable[Any]],
        automated: bool = False,
        detector: str | None = None,
        metadata: dict | None = None,
        state_check: Callable[[], None] | None = None,
    ) -> CaseRecord:
        self.permissions.require_guild(guild)
        created_at = utc_now()
        record = CaseRecord(
            guild_id=guild.id,
            target_user_id=getattr(target, "id", 0),
            moderator_user_id=getattr(moderator, "id", 0),
            action=action,
            reason=reason or "",
            created_at=created_at,
            status=STATUS_FAILED,
            duration_seconds=duration_seconds,
            expires_at=(
                created_at + timedelta(seconds=duration_seconds) if duration_seconds else None
            ),
            automated=automated,
            detector=detector,
            metadata=metadata,
        )
        try:
            cleaned = validate_reason(reason)
            self.permissions.require_state(action, duration_seconds=duration_seconds)
            if state_check is not None:
                state_check()
            # Automated actions have no human moderator: the bot's own
            # permission is verified by ``require_bot_permissions`` below, so
            # the moderator-permission gate is skipped (there is no moderator).
            if not automated:
                self.permissions.require_moderator(moderator, action)
            await self.permissions.require_bot_permissions(guild, action)
            self.permissions.require_target(target, guild, moderator, member_required=True)
            await self.permissions.require_hierarchy(moderator, target, guild)
        except ModerationError as exc:
            self._record_failure(record, exc)
            raise
        record = replace(record, reason=cleaned)
        try:
            await execute()
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.info(
                "discord rejected action=%s guild=%s target=%s error=%s",
                action,
                guild.id,
                getattr(target, "id", None),
                type(exc).__name__,
            )
            self._record_failure(record, ModerationExecutionError())
            raise ModerationExecutionError() from None
        return await self._record_success(record, target=target, guild=guild, moderator=moderator)

    # ------------------------------------------------------------ persistence

    def _record_failure(self, record: CaseRecord, error: ModerationError) -> None:
        """Persist a failed case; never masks the caller's original error."""
        failed = replace(record, status=STATUS_FAILED, error=error.user_message)
        try:
            stored = self.case_service.create(failed)
        except Exception:  # noqa: BLE001 - persistence must not mask the original error
            logger.exception(
                "case persistence failed for a failed action: action=%s guild=%s "
                "moderator=%s target=%s",
                record.action,
                record.guild_id,
                record.moderator_user_id,
                record.target_user_id,
            )
        else:
            self._audit(stored)

    async def _record_success(
        self,
        record: CaseRecord,
        *,
        target: Any,
        guild: discord.Guild,
        moderator: discord.Member,
    ) -> CaseRecord:
        success_record = replace(record, status=STATUS_SUCCESS)
        try:
            stored = self.case_service.create(success_record)
        except Exception as exc:  # noqa: BLE001 - see persistence contract in docstring
            logger.exception(
                "case persistence failed after the action succeeded: action=%s guild=%s "
                "moderator=%s target=%s",
                record.action,
                record.guild_id,
                record.moderator_user_id,
                record.target_user_id,
            )
            # The action happened on Discord; never claim it failed. Tell the
            # user the truth and let them surface it to administrators.
            raise CasePersistenceError() from exc
        self._audit(stored)
        if stored.action in PUNISHMENT_ACTIONS and self._notifications_enabled(guild):
            delivered = await self.notifier.notify_punishment(
                target,
                stored.action,
                stored.reason,
                guild.name,
                case_id=stored.case_id,
                duration_seconds=stored.duration_seconds,
            )
            if not delivered:
                # The action still succeeded; record that the DM could not be
                # delivered so the command layer can tell the moderator
                # privately (spec: a failed DM never fails the action).
                stored = replace(
                    stored,
                    metadata={
                        **(stored.metadata or {}),
                        "dm_delivered": False,
                    },
                )
                if stored.case_id is not None:
                    updated = self.case_service.update_metadata(
                        stored.guild_id, stored.case_id, stored.metadata
                    )
                    if updated is not None:
                        stored = updated
        await self._post_to_log_channel(guild, stored, target=target, moderator=moderator)
        return stored

    # ------------------------------------------------------------------ audit

    def _audit(self, record: CaseRecord) -> None:
        """Structured audit log line (Phase 6: includes the reason, truncated).

        The reason is moderation metadata, not a message content; it is
        truncated and newline-stripped to keep log lines bounded. The log
        redaction filter still scrubs token-shaped strings defensively.
        Never includes secrets.
        """
        duration = f" duration={record.duration_seconds}" if record.duration_seconds else ""
        reason = " ".join((record.reason or "").split())[:80]
        logger.info(
            "case=%s action=%s guild=%s moderator=%s target=%s result=%s reason=%s%s",
            record.case_id,
            record.action,
            record.guild_id,
            record.moderator_user_id,
            record.target_user_id,
            record.status,
            reason,
            duration,
        )

    # ---------------------------------------------------------- log channel

    async def post_event(
        self,
        guild: discord.Guild,
        *,
        title: str,
        fields: list[tuple[str, str]],
        timestamp: Any | None = None,
    ) -> None:
        """Best-effort embed to the guild's configured log channel (if any).

        Consults the per-guild configuration (log channel + mod-log toggle)
        when a config service is present, otherwise the environment settings.
        Failure to post (missing channel, missing permission, Discord error)
        is logged and never fails the caller — this is the shared poster for
        moderation actions and configuration changes.
        """
        config = self._guild_config(guild)
        if config is not None:
            if not config.mod_log_enabled:
                return
            log_channel_id = config.log_channel_id
        else:
            log_channel_id = self.settings.log_channel_id
        if log_channel_id is None:
            return
        channel = guild.get_channel(log_channel_id)
        if channel is None:
            logger.info(
                "log channel %s not found in guild %s",
                log_channel_id,
                guild.id,
            )
            return
        embed = discord.Embed(title=title, timestamp=timestamp or utc_now())
        for name, value in fields:
            embed.add_field(name=name, value=value, inline=False)
        try:
            await channel.send(embed=embed)
        except Exception as exc:  # noqa: BLE001 - non-fatal by design
            logger.info(
                "log channel post failed: guild=%s error=%s",
                guild.id,
                type(exc).__name__,
            )

    async def _post_to_log_channel(
        self,
        guild: discord.Guild,
        record: CaseRecord,
        *,
        target: Any,
        moderator: discord.Member,
    ) -> None:
        """Best-effort summary embed to the configured log channel (if any)."""
        fields = [
            ("Target", getattr(target, "mention", str(record.target_user_id))),
            ("Moderator", getattr(moderator, "mention", str(record.moderator_user_id))),
            ("Reason", record.reason or "—"),
        ]
        if record.duration_seconds is not None:
            fields.append(("Duration", f"{record.duration_seconds}s"))
        fields.append(("Status", "Success" if record.success else "Failed"))
        if record.automated:
            fields.append(("Source", f"Automated ({record.detector or 'detector'})"))
        await self.post_event(
            guild,
            title=f"Case #{record.case_id} — {record.action.title()}",
            fields=fields,
            timestamp=record.created_at,
        )

    # ---------------------------------------------------------------- helpers

    def max_purge_amount_for(self, guild: discord.Guild) -> int:
        """The maximum purge amount for ``guild`` (per-guild config wins)."""
        config = self._guild_config(guild)
        if config is not None:
            return config.max_purge_amount
        return self.settings.max_purge_amount

    def _notifications_enabled(self, guild: discord.Guild) -> bool:
        """Whether DM notifications are enabled for ``guild`` (per-guild wins)."""
        config = self._guild_config(guild)
        if config is not None:
            return config.notify_users
        return self.settings.notify_users

    def _guild_config(self, guild: discord.Guild) -> Any | None:
        """Per-guild configuration snapshot, or ``None`` without a config service.

        Never raises: configuration problems degrade to the environment
        defaults with a logged diagnostic, so moderation is never blocked by
        a config read failure.
        """
        if self.config_service is None or guild is None:
            return None
        try:
            return self.config_service.get(guild.id)
        except Exception:  # noqa: BLE001 - non-fatal by design
            logger.exception(
                "could not load per-guild config for guild %s",
                getattr(guild, "id", None),
            )
            return None

    async def _fetch_user(self, user_id: int) -> discord.User:
        """Fetch a user by ID, raising a safe error when they don't exist."""
        try:
            return await self.bot.fetch_user(user_id)
        except discord.NotFound:
            raise InvalidTargetError("That user could not be found.") from None
