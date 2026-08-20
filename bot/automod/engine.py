"""The automated moderation engine.

Wires the Phase 4 pipeline together:

    Discord event -> normalization -> exemption checks -> detectors ->
    moderation decision -> enforcement policy -> case service -> audit logging

The engine is deliberately the **only** place that knows both sides of the
detection/enforcement split: detectors (``bot.automod.detectors``) only
report findings; the engine decides the action from configuration and
executes it through the existing
:class:`bot.services.moderation.ModerationService`, which owns case creation,
audit logging, log-channel posts, and DM notifications. No detection logic
lives in Discord event handlers — ``RiyxoenBot.on_message`` simply calls
:meth:`AutomodEngine.handle_message`.

Phase 5: configuration is **per guild**. The engine reads each guild's
snapshot through the :class:`bot.services.guild_config.GuildConfigService`
(never storage directly), and keeps a bounded cache of per-guild detector
sets so the stateful detectors' history survives between messages. A
``/config`` change invalidates the guild's detector set via
:meth:`invalidate_guild`, so new thresholds apply to the next message. The
environment ``RIYXOEN_AUTOMOD_ENABLED`` remains the master switch (it also
drives the message-content intent); a guild additionally needs
``automod_enabled`` in its own configuration.

Safety properties:

- **Never raises into the event loop** — ``handle_message`` catches everything
  and logs, so a detector or enforcement hiccup can never crash the client.
- **One action per message, one case per event** — the first detector that
  fires wins, and the enforcement cooldown prevents a spam burst from
  producing dozens of cases.
- **No dangerous automated actions** — automated enforcement is limited to
  delete / warn / timeout; ban and kick are intentionally not automated in
  this phase.
- **False-positive aware** — the engine is off by default; exempt authors and
  channels are skipped before any detector runs; warn actions escalate to
  timeouts only when configured thresholds are met.
- **Bounded memory** — detector history is bounded per user and globally; the
  per-guild detector-set cache is capped; cooldowns are capped and
  self-cleaning.
"""

from __future__ import annotations

import inspect
import logging
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from bot.automod.cooldowns import CooldownTracker
from bot.automod.detectors import (
    Detection,
    Detector,
    DuplicateDetector,
    InviteDetector,
    LinkDetector,
    MentionDetector,
    RaidDetector,
    SpamDetector,
    WordFilterDetector,
)
from bot.automod.exemptions import ExemptionChecker
from bot.automod.normalize import normalize_text
from bot.configuration.automod import (
    ACTION_ALERT,
    ACTION_ALLOW,
    ACTION_DELETE,
    ACTION_TIMEOUT,
    ACTION_WARN,
)
from bot.configuration.guild import GuildConfig, default_guild_config
from bot.configuration.settings import Settings
from bot.moderation.cases import utc_now
from bot.moderation.errors import ModerationError
from bot.permissions.checks import PermissionChecker
from bot.services.cases import CaseService
from bot.services.moderation import ModerationService

logger = logging.getLogger("riyxoen.automod")

#: Run a full state sweep every N analyzed messages (stale-key hygiene).
PRUNE_EVERY_MESSAGES = 500

#: Upper bound on cached per-guild detector sets (bounded memory; bots are
#: rarely in more than a handful of guilds, and stale sets are evicted
#: oldest-first and rebuilt from the next message's configuration).
MAX_DETECTOR_SETS = 32


@dataclass(frozen=True, slots=True)
class _DetectorSet:
    """The detectors + action policy for one guild's configuration."""

    detectors: list[Detector]
    actions: dict[str, str]
    skipped: frozenset[str]


@dataclass(frozen=True, slots=True)
class _GuildProbe:
    """Minimal message-shaped stand-in for the raid detector's guild lookup."""

    guild: Any


class AutomodEngine:
    """Runs detectors and enforces configured actions for guild messages."""

    def __init__(
        self,
        settings: Settings,
        case_service: CaseService,
        moderation_service: ModerationService,
        permissions: PermissionChecker,
        config_service: Any,
        *,
        clock: Callable[[], Any] | None = None,
    ) -> None:
        self.settings = settings
        self.case_service = case_service
        self.moderation_service = moderation_service
        self.config_service = config_service
        self.exemptions = ExemptionChecker(permissions)
        self.cooldowns = CooldownTracker(clock=clock)
        self._clock = clock or utc_now
        self._message_count = 0
        #: Bounded per-guild detector-set cache (OrderedDict, LRU eviction).
        self._detector_sets: OrderedDict[int, _DetectorSet] = OrderedDict()

    # ---------------------------------------------------------- properties

    @property
    def active_detector_names(self) -> tuple[str, ...]:
        """Names of detectors active under the *seeded defaults* (used for the
        startup log before any guild configuration exists)."""
        defaults = default_guild_config(self.settings, guild_id=0)
        _detectors, actions, skipped = self._build_detectors(defaults)
        return tuple(name for name in actions if name not in skipped)

    # ------------------------------------------------------------- pipeline

    async def handle_message(self, message: Any) -> None:
        """Entry point from the Discord event handler. Never raises."""
        try:
            await self._handle(message)
        except Exception:  # noqa: BLE001 - must never break message handling
            logger.exception(
                "automod failed to process message guild=%s channel=%s author=%s",
                getattr(getattr(message, "guild", None), "id", None),
                getattr(getattr(message, "channel", None), "id", None),
                getattr(getattr(message, "author", None), "id", None),
            )

    # ------------------------------------------------------------ member joins

    async def handle_member_join(self, guild: Any, member: Any) -> None:
        """Entry point from the Discord member-join event. Never raises.

        Runs the conservative raid detector: join bursts are detected from
        timestamps only (no message content, no privileged data), and the
        configured action is conservative by default (``alert`` — a notice to
        the log channel). ``timeout`` is opt-in and only touches members who
        joined within the current window; ban/kick are never raid actions.
        """
        try:
            await self._handle_join(guild, member)
        except Exception:  # noqa: BLE001 - must never break the event loop
            logger.exception(
                "automod failed to process member join guild=%s member=%s",
                getattr(guild, "id", None),
                getattr(member, "id", None),
            )

    async def _handle_join(self, guild: Any, member: Any) -> None:
        if not self.settings.automod.enabled:
            return
        if guild is None or member is None or getattr(member, "bot", False):
            return
        config = self.config_service.get(guild.id)
        if not config.automod_enabled:
            return

        detector_set = self._get_detectors(guild.id, config)
        raid_detector = self._find_detector(detector_set, "raid")
        if raid_detector is None:
            return
        raid_detector.record_join(guild.id, member.id)
        detection = raid_detector.analyze(_GuildProbe(guild), "")
        if detection is None:
            return

        action = detector_set.actions.get("raid", ACTION_ALERT)
        key = (guild.id, 0)  # one cooldown per guild for the join burst
        if self.cooldowns.is_active(key):
            return
        await self._enforce_raid(guild, detection, action)
        self.cooldowns.start(key, config.enforcement_cooldown_seconds)

    @staticmethod
    def _find_detector(detector_set: _DetectorSet, name: str) -> Detector | None:
        """Return a detector by name from the cached set (or ``None``)."""
        for detector in detector_set.detectors:
            if detector.name == name:
                return detector
        return None

    async def _enforce_raid(
        self,
        guild: Any,
        detection: Detection,
        action: str,
    ) -> None:
        """Enforce the raid action: alert (log-channel notice) or timeout.

        ``alert`` never punishes — it posts a notice to the guild's
        configured log channel when mod logs are enabled and records an audit
        line. ``timeout`` is opt-in and applies the configured default
        timeout to members who joined within the window.
        """
        if action == ACTION_ALERT:
            await self.moderation_service.post_event(
                guild,
                title="Raid protection alert",
                fields=[
                    ("Detector", "raid"),
                    ("Reason", detection.reason),
                    ("Joins", str(self._recent_joins(guild))),
                ],
            )
            logger.info(
                "automod raid alert: guild=%s detector=%s",
                guild.id,
                detection.detector,
            )
            return
        if action == ACTION_TIMEOUT:
            bot_member = getattr(guild, "me", None)
            if bot_member is None:
                logger.info(
                    "automod: bot member unavailable; skipping raid timeout guild=%s", guild.id
                )
                return
            duration = self._guild_config(guild.id).timeout_duration_seconds
            for user_id in self._recent_joins(guild):
                target = self._resolve_member(guild, user_id)
                if target is None:
                    continue
                if inspect.iscoroutine(target):
                    try:
                        target = await target
                    except Exception:  # noqa: BLE001 - best-effort resolution
                        logger.info(
                            "automod: could not resolve raid joiner guild=%s user=%s",
                            guild.id,
                            user_id,
                        )
                        continue
                if target is None:
                    continue
                try:
                    await self.moderation_service.timeout(
                        guild,
                        bot_member,
                        target,
                        duration,
                        f"Automated moderation: {detection.reason}",
                        automated=True,
                        detector=detection.detector,
                    )
                except ModerationError as exc:
                    logger.info(
                        "automod raid timeout failed: guild=%s user=%s error=%s",
                        guild.id,
                        user_id,
                        exc.user_message,
                    )
            return
        logger.warning("automod: unknown raid action %r; skipping", action)

    def _guild_config(self, guild_id: int) -> GuildConfig:
        return self.config_service.get(guild_id)

    def _recent_joins(self, guild: Any) -> list[int]:
        """User IDs of members who joined within the current raid window."""
        detector_set = self._detector_sets.get(guild.id)
        if detector_set is None:
            return []
        raid = self._find_detector(detector_set, "raid")
        if raid is None:
            return []
        return raid.recent_joiners(guild.id)

    def _resolve_member(self, guild: Any, user_id: int) -> Any | None:
        """Resolve a user ID to a guild member object (best-effort)."""
        getter = getattr(guild, "get_member", None)
        member = getter(user_id) if callable(getter) else None
        if member is None:
            fetch = getattr(guild, "fetch_member", None)
            if callable(fetch):
                try:
                    return fetch(user_id)  # may be a coroutine; see call site
                except Exception:  # noqa: BLE001 - best-effort
                    return None
        return member

    async def _handle(self, message: Any) -> None:
        # Environment master switch: also drives the message-content intent,
        # so per-guild config can only enable automation within it.
        if not self.settings.automod.enabled:
            return
        guild = message.guild
        if guild is None:
            return
        author = message.author
        if author is None or getattr(author, "bot", False):
            return
        content = getattr(message, "content", "") or ""
        if not content:
            return

        config = self.config_service.get(guild.id)
        if not config.automod_enabled:
            return
        if self.exemptions.is_exempt(message, config):
            return

        normalized = normalize_text(content)
        detector_set = self._get_detectors(guild.id, config)
        detection = self._detect(message, normalized, detector_set)
        if detection is None:
            self._maybe_prune()
            return

        key = (guild.id, author.id)
        if self.cooldowns.is_active(key):
            logger.info(
                "automod: enforcement cooldown active; skipping guild=%s user=%s detector=%s",
                guild.id,
                author.id,
                detection.detector,
            )
            self._maybe_prune()
            return

        action = detector_set.actions.get(detection.detector, ACTION_ALLOW)
        if action == ACTION_ALLOW:
            return

        duration: int | None = None
        if action == ACTION_WARN:
            escalated = self._escalate_warning(message, detection, config)
            if escalated is not None:
                action, duration = escalated
        elif action == ACTION_TIMEOUT:
            # Detectors configured with a plain "timeout" action use the
            # configured default duration (escalation overrides above).
            duration = config.timeout_duration_seconds

        await self._enforce(message, detection, action, duration)
        # Arm the cooldown after any enforcement attempt so a burst produces
        # one case, not dozens (see docstring).
        self.cooldowns.start(key, config.enforcement_cooldown_seconds)
        self._maybe_prune()

    # ------------------------------------------------------------- decision

    def _get_detectors(self, guild_id: int, config: GuildConfig) -> _DetectorSet:
        """Return the cached detector set for ``guild_id``, building it from
        the guild's configuration when absent or invalidated."""
        cached = self._detector_sets.get(guild_id)
        if cached is not None:
            return cached
        detectors, actions, skipped = self._build_detectors(config)
        detector_set = _DetectorSet(detectors, actions, frozenset(skipped))
        self._detector_sets[guild_id] = detector_set
        while len(self._detector_sets) > MAX_DETECTOR_SETS:
            self._detector_sets.popitem(last=False)
        return detector_set

    def invalidate_guild(self, guild_id: int) -> None:
        """Drop the cached detector set for ``guild_id``.

        Called by the configuration service's invalidation listeners after a
        ``/config`` change; the next message rebuilds detectors from the new
        configuration, so stale settings never persist.
        """
        self._detector_sets.pop(guild_id, None)

    def _build_detectors(
        self, config: GuildConfig
    ) -> tuple[list[Detector], dict[str, str], set[str]]:
        """Build the five detectors + action policy from a configuration
        snapshot. Detectors are pure (they never enforce); the engine owns
        the clock and injects it into the stateful ones so windows and
        cooldowns share one time source (tests inject a fake clock)."""
        detectors: list[Detector] = []
        actions: dict[str, str] = {}

        spam = SpamDetector(
            threshold=config.spam_threshold,
            window_seconds=config.spam_window_seconds,
            clock=self._clock,
        )
        detectors.append(spam)
        actions["spam"] = config.spam_action

        duplicate = DuplicateDetector(
            threshold=config.duplicate_threshold,
            window_seconds=config.duplicate_window_seconds,
            clock=self._clock,
        )
        detectors.append(duplicate)
        actions["duplicate"] = config.duplicate_action

        mentions = MentionDetector(
            user_threshold=config.mention_user_threshold,
            role_threshold=config.mention_role_threshold,
            total_threshold=config.mention_total_threshold,
            everyone_threshold=config.mention_everyone_threshold,
        )
        detectors.append(mentions)
        actions["mentions"] = config.mention_action

        links = LinkDetector(allowed_domains=config.allowed_domains)
        detectors.append(links)
        actions["links"] = config.link_action

        word_filter = WordFilterDetector(
            terms=config.blocked_terms, substring=config.blocked_terms_substring
        )
        detectors.append(word_filter)
        actions["word_filter"] = config.word_filter_action

        invites = InviteDetector(allowed_codes=config.invite_allowed_codes)
        detectors.append(invites)
        actions["invites"] = config.invite_action

        raid = RaidDetector(
            threshold=config.raid_join_threshold,
            window_seconds=config.raid_window_seconds,
            clock=self._clock,
        )
        detectors.append(raid)
        actions["raid"] = config.raid_action

        # Detectors whose configured action is "allow" are never consulted
        # (raid's conservative "alert" is always meaningful, so it is never
        # skipped by this rule).
        skipped = {name for name, action in actions.items() if action == ACTION_ALLOW}
        return detectors, actions, skipped

    def _detect(
        self, message: Any, normalized: str, detector_set: _DetectorSet
    ) -> Detection | None:
        """Run detectors in order; the first finding wins (one case per message)."""
        for detector in detector_set.detectors:
            if detector.name in detector_set.skipped:
                continue
            detection = detector.analyze(message, normalized)
            if detection is not None:
                return detection
        return None

    def _escalate_warning(
        self, message: Any, detection: Detection, config: GuildConfig
    ) -> tuple[str, int] | None:
        """Decide whether a warn should escalate to a timeout.

        Counts the member's **active** warnings (successful warn cases in this
        guild, created within the configured warning-expiration window) and
        applies the highest configured ``(warning_count, timeout_seconds)``
        pair whose threshold the new warning would reach. Warning expiration
        is documented: a warning stops counting once older than
        ``warning_window_seconds``; ``0`` means warnings never expire.
        """
        escalation = config.escalation
        if not escalation:
            return None

        since = None
        if config.warning_window_seconds > 0:
            since = self._clock() - timedelta(seconds=config.warning_window_seconds)
        count = self.case_service.count_active_warnings(message.guild.id, message.author.id, since)
        next_count = count + 1
        chosen: tuple[int, int] | None = None
        for threshold, duration in escalation:  # sorted ascending by config
            if threshold <= next_count:
                chosen = (threshold, duration)
            else:
                break
        if chosen is None:
            return None
        return (ACTION_TIMEOUT, chosen[1])

    # ------------------------------------------------------------ enforcement

    async def _enforce(
        self,
        message: Any,
        detection: Detection,
        action: str,
        duration: int | None,
    ) -> None:
        guild = message.guild
        bot_member = getattr(guild, "me", None)
        if bot_member is None:
            logger.info(
                "automod: bot member unavailable; skipping enforcement guild=%s",
                guild.id,
            )
            return
        target = message.author
        reason = f"Automated moderation: {detection.reason}"
        try:
            if action == ACTION_DELETE:
                await self.moderation_service.delete_message(
                    guild,
                    bot_member,
                    message,
                    reason,
                    automated=True,
                    detector=detection.detector,
                )
            elif action == ACTION_WARN:
                await self.moderation_service.warn(
                    guild,
                    bot_member,
                    target,
                    reason,
                    automated=True,
                    detector=detection.detector,
                )
            elif action == ACTION_TIMEOUT and duration is not None:
                await self.moderation_service.timeout(
                    guild,
                    bot_member,
                    target,
                    duration,
                    reason,
                    automated=True,
                    detector=detection.detector,
                )
            else:
                logger.warning("automod: unknown action %r; skipping", action)
                return
        except ModerationError as exc:
            # The service already recorded a failed case and audit line; the
            # engine logs the safe summary and moves on (never raises).
            logger.info(
                "automod enforcement failed: guild=%s user=%s detector=%s action=%s error=%s",
                guild.id,
                target.id,
                detection.detector,
                action,
                exc.user_message,
            )
            return
        logger.info(
            "automod enforced: guild=%s user=%s detector=%s action=%s",
            guild.id,
            target.id,
            detection.detector,
            action,
        )

    # --------------------------------------------------------------- hygiene

    def _maybe_prune(self) -> None:
        """Periodically sweep stale state so memory stays bounded over time."""
        self._message_count += 1
        if self._message_count % PRUNE_EVERY_MESSAGES == 0:
            now = self._clock()
            for detector_set in self._detector_sets.values():
                for detector in detector_set.detectors:
                    detector.prune(now)
            self.cooldowns.prune()
