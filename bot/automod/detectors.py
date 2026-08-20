"""Detectors for automated moderation.

Each detector implements a small interface:

    ``analyze(message, normalized) -> Detection | None``

and is **stateless with respect to enforcement**: a detector never bans,
kicks, timeouts, or deletes anything — it only reports a finding. The engine
turns a :class:`Detection` into an action through the enforcement policy.

``normalized`` is the message text pre-normalized once by the engine (see
:mod:`bot.automod.normalize`), so detectors never re-normalize per detector.

Memory: only the frequency-based detectors (spam, duplicate) keep state, and
it is strictly bounded (:class:`bot.automod.state.BoundedUserHistory`). The
other three detectors are stateless.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from bot.automod.normalize import (
    extract_hosts,
    is_domain_allowed,
    normalize_text,
    term_pattern,
)
from bot.automod.state import BoundedUserHistory
from bot.moderation.cases import utc_now

#: Default cap on tracked ``(guild, user)`` keys per frequency detector.
DEFAULT_MAX_TRACKED_USERS = 5000


@dataclass(frozen=True, slots=True)
class Detection:
    """A detector's finding.

    Contains only the canonical detector name and a safe, short reason (no
    message contents, no user input, no internal detail). The engine uses the
    ``detector`` name to select the configured enforcement action.
    """

    detector: str
    reason: str


class Detector(ABC):
    """Base class for all detectors. ``analyze`` must never raise for input
    it does not understand — unknown shapes simply yield no detection."""

    name: str = ""

    @abstractmethod
    def analyze(self, message: Any, normalized: str) -> Detection | None:
        """Analyze ``message`` and return a :class:`Detection` or ``None``."""

    def prune(self, now: datetime | None = None) -> None:
        """Release stale state (no-op for stateless detectors)."""
        return None


# ------------------------------------------------------------------- spam


class SpamDetector(Detector):
    """Detects a burst of messages from one user within a time window.

    Tracks message timestamps per ``(guild, user)`` in a bounded history.
    Triggers when ``threshold`` messages arrive within ``window`` seconds.
    """

    name = "spam"

    def __init__(
        self,
        *,
        threshold: int,
        window_seconds: int,
        max_users: int = DEFAULT_MAX_TRACKED_USERS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.threshold = max(int(threshold), 2)
        self.window = timedelta(seconds=max(int(window_seconds), 1))
        self._clock = clock or utc_now
        self.per_user = min(max(self.threshold * 2, 8), 200)
        self._history = BoundedUserHistory(max_users=max_users, per_user=self.per_user)

    @property
    def tracked_users(self) -> int:
        """Number of ``(guild, user)`` keys currently tracked (memory bound)."""
        return len(self._history)

    def analyze(self, message: Any, normalized: str) -> Detection | None:
        now = self._clock()
        key = (message.guild.id, message.author.id)
        cutoff = now - self.window
        self._history.prune(key, cutoff)
        self._history.add(key, None, now)
        if len(self._history.snapshot(key)) >= self.threshold:
            return Detection("spam", "spam detected")
        return None

    def prune(self, now: datetime | None = None) -> None:
        self._history.prune_all((now or self._clock()) - self.window)


# --------------------------------------------------------------- duplicate


class DuplicateDetector(Detector):
    """Detects repeated identical (normalized) messages from one user.

    Messages are compared by normalized text, so case, punctuation, and
    spacing variants count as duplicates. Only messages within ``window``
    seconds count. A single repeated message never triggers — the threshold
    defaults to 4 and is configurable.
    """

    name = "duplicate"

    def __init__(
        self,
        *,
        threshold: int,
        window_seconds: int,
        max_users: int = DEFAULT_MAX_TRACKED_USERS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.threshold = max(int(threshold), 2)
        self.window = timedelta(seconds=max(int(window_seconds), 1))
        self._clock = clock or utc_now
        self.per_user = min(max(self.threshold * 4, 8), 400)
        self._history = BoundedUserHistory(max_users=max_users, per_user=self.per_user)

    @property
    def tracked_users(self) -> int:
        """Number of ``(guild, user)`` keys currently tracked (memory bound)."""
        return len(self._history)

    def analyze(self, message: Any, normalized: str) -> Detection | None:
        if not normalized:
            return None
        now = self._clock()
        key = (message.guild.id, message.author.id)
        cutoff = now - self.window
        self._history.prune(key, cutoff)
        self._history.add(key, normalized, now)
        texts = [text for text, _ in self._history.snapshot(key)]
        if texts.count(normalized) >= self.threshold:
            return Detection("duplicate", "duplicate messages detected")
        return None

    def prune(self, now: datetime | None = None) -> None:
        self._history.prune_all((now or self._clock()) - self.window)


# ----------------------------------------------------------------- mentions


class MentionDetector(Detector):
    """Detects excessive mentions.

    User, role, @everyone/@here, and total mention dimensions each have an
    independent threshold; ``0`` disables that dimension. ``@everyone`` and
    ``@here`` get their own dimension because Discord permissions already
    gate them — servers that want to allow them keep the threshold at 0.
    """

    name = "mentions"

    def __init__(
        self,
        *,
        user_threshold: int = 0,
        role_threshold: int = 0,
        total_threshold: int = 0,
        everyone_threshold: int = 0,
    ) -> None:
        self.user_threshold = max(int(user_threshold), 0)
        self.role_threshold = max(int(role_threshold), 0)
        self.total_threshold = max(int(total_threshold), 0)
        self.everyone_threshold = max(int(everyone_threshold), 0)

    def analyze(self, message: Any, normalized: str) -> Detection | None:
        user_mentions = len(getattr(message, "mentions", None) or ())
        role_mentions = len(getattr(message, "role_mentions", None) or ())
        everyone = 1 if getattr(message, "mention_everyone", False) else 0
        total = user_mentions + role_mentions + everyone

        if self.user_threshold and user_mentions >= self.user_threshold:
            return Detection("mentions", "excessive user mentions detected")
        if self.role_threshold and role_mentions >= self.role_threshold:
            return Detection("mentions", "excessive role mentions detected")
        if self.everyone_threshold and everyone >= self.everyone_threshold:
            return Detection("mentions", "excessive @everyone/@here mentions detected")
        if self.total_threshold and total >= self.total_threshold:
            return Detection("mentions", "excessive mentions detected")
        return None


# ------------------------------------------------------------------- links


class LinkDetector(Detector):
    """Detects URLs that are not on the configured allowlist.

    Enforcement is deliberately not automatic: the engine maps a finding to
    the configured ``link_action`` (``allow`` = no enforcement). Domain
    matching is exact-or-proper-subdomain so lookalike domains never match
    (see :func:`bot.automod.normalize.is_domain_allowed`).
    """

    name = "links"

    def __init__(self, *, allowed_domains: tuple[str, ...] = ()) -> None:
        self.allowed_domains = tuple(domain for domain in allowed_domains if domain)

    def analyze(self, message: Any, normalized: str) -> Detection | None:
        content = getattr(message, "content", "") or ""
        hosts = extract_hosts(content)
        if not hosts:
            return None
        for host in hosts:
            if not is_domain_allowed(host, self.allowed_domains):
                return Detection("links", "URL not allowed")
        return None


# ------------------------------------------------------------------ invites


#: Discord invite URL pattern: ``discord.gg`` short links and the canonical
#: ``/invite/`` paths on discord.com / discordapp.com; the code is the path
#: segment after the slash (letters, digits, underscores, hyphens). The
#: ``\\b`` before each domain prevents lookalikes (``notdiscord.com``,
#: ``discord.gg.evil.com``) from matching.
_INVITE_PATTERN = re.compile(
    r"\b(?:discord\.gg|discord\.com/invite|discordapp\.com/invite)/([a-zA-Z0-9_-]+)"
)


class InviteDetector(Detector):
    """Detects Discord invite links in messages.

    Invites are a dedicated detector rather than a special case of the link
    detector: a server may allow arbitrary URLs while still blocking invites
    (or vice versa). Per-guild configuration supplies the enforcement action
    and an optional allowlist of invite **codes** (server owners whitelist
    their own invite links; non-matching invites are detected). Detection is
    conservative — only full ``discord.gg/<code>`` / ``/invite/<code>`` forms
    are matched.
    """

    name = "invites"

    def __init__(self, *, allowed_codes: tuple[str, ...] = ()) -> None:
        self.allowed_codes = tuple(code for code in allowed_codes if code)

    def analyze(self, message: Any, normalized: str) -> Detection | None:
        content = getattr(message, "content", "") or ""
        if not content:
            return None
        lowered = content.lower()
        for match in _INVITE_PATTERN.finditer(lowered):
            code = match.group(1).lower()
            if code and code not in self.allowed_codes:
                return Detection("invites", "discord invite detected")
        return None


# ------------------------------------------------------------ raid protection


#: Upper bound on guilds whose join history is tracked (memory safety).
MAX_RAID_GUILDS = 500
#: Upper bound on stored join timestamps per guild.
MAX_JOINS_PER_GUILD = 200


class RaidDetector(Detector):
    """Conservatively detects join bursts per guild.

    Records member-join timestamps in a **bounded** per-guild history and
    reports a finding when more than ``threshold`` joins land within
    ``window_seconds``. By design it never recommends destructive actions on
    its own: the engine maps the finding to the configured action
    (``alert`` by default — a notice to the log channel — or ``timeout``),
    and ban/kick are not raid actions.

    Memory is bounded: at most :data:`MAX_RAID_GUILDS` guilds are tracked,
    each holding at most :data:`MAX_JOINS_PER_GUILD` timestamps, and stale
    entries are pruned on access plus by the engine's periodic sweep.
    """

    name = "raid"

    def __init__(
        self,
        *,
        threshold: int,
        window_seconds: int,
        max_guilds: int = MAX_RAID_GUILDS,
        max_joins: int = MAX_JOINS_PER_GUILD,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.threshold = max(int(threshold), 2)
        self.window = timedelta(seconds=max(int(window_seconds), 1))
        self.max_guilds = max(max_guilds, 1)
        self.max_joins = max(max_joins, 1)
        self._clock = clock or utc_now
        self._joins: dict[int, deque[tuple[int, datetime]]] = {}

    @property
    def tracked_guilds(self) -> int:
        """Number of guilds currently tracked (memory bound)."""
        return len(self._joins)

    def record_join(self, guild_id: int, user_id: int, now: datetime | None = None) -> None:
        """Record a member join. Call before :meth:`analyze` for the event."""
        timestamp = now or self._clock()
        joins = self._joins.get(guild_id)
        if joins is None:
            if len(self._joins) >= self.max_guilds:
                self._evict_oldest_guild()
            joins = deque(maxlen=self.max_joins)
            self._joins[guild_id] = joins
        joins.append((user_id, timestamp))

    def recent_joiners(self, guild_id: int) -> list[int]:
        """User IDs of joins within the current window (newest last)."""
        self.prune(guild_id=guild_id)
        joins = self._joins.get(guild_id) or ()
        return [user_id for user_id, _ts in joins]

    def analyze(self, message: Any, normalized: str) -> Detection | None:
        """Report a raid when the recent join count crosses the threshold.

        ``message`` is only used for its guild; the join was recorded via
        :meth:`record_join` before this call. Never raises for unknown input.
        """
        guild_id = getattr(getattr(message, "guild", None), "id", None)
        if guild_id is None:
            return None
        self.prune(guild_id=guild_id)
        joins = self._joins.get(guild_id)
        if joins is not None and len(joins) >= self.threshold:
            return Detection("raid", "join burst detected")
        return None

    def prune(self, now: datetime | None = None, guild_id: int | None = None) -> None:
        """Drop joins older than the window.

        With ``guild_id``, prunes that guild only; otherwise sweeps every
        tracked guild (called by the engine's periodic hygiene pass).
        """
        cutoff = (now or self._clock()) - self.window
        targets = [guild_id] if guild_id is not None else list(self._joins)
        for target in targets:
            joins = self._joins.get(target)
            if joins is None:
                continue
            while joins and joins[0][1] < cutoff:
                joins.popleft()
            if not joins:
                del self._joins[target]

    def _evict_oldest_guild(self) -> None:
        oldest = next(iter(self._joins))
        del self._joins[oldest]


# -------------------------------------------------------------- word filter


class WordFilterDetector(Detector):
    """Detects configured blocked terms in normalized text.

    Terms come from configuration only — offensive words are never hardcoded
    in source. Matching is whole-word by default (``ass`` does not match
    ``assessment``); substring matching is available via ``substring=True``
    for servers that explicitly opt into it.
    """

    name = "word_filter"

    def __init__(self, *, terms: tuple[str, ...] = (), substring: bool = False) -> None:
        normalized_terms = tuple(
            dict.fromkeys(normalize_text(term) for term in terms if normalize_text(term))
        )
        self.substring = substring
        self._terms = normalized_terms
        self._patterns = [term_pattern(term) for term in normalized_terms]

    def analyze(self, message: Any, normalized: str) -> Detection | None:
        if not normalized or not self._terms:
            return None
        if self.substring:
            matched = any(term in normalized for term in self._terms)
        else:
            matched = any(pattern.search(normalized) is not None for pattern in self._patterns)
        if matched:
            return Detection("word_filter", "blocked term detected")
        return None
