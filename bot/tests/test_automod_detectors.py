"""Detector unit tests.

Detectors are tested in isolation, without Discord and without any
enforcement: they only ever return a :class:`Detection` or ``None``. Stateful
detectors use an injectable clock and bounded histories so memory behavior is
assertable too.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from bot.automod.detectors import (
    DuplicateDetector,
    InviteDetector,
    LinkDetector,
    MentionDetector,
    RaidDetector,
    SpamDetector,
    WordFilterDetector,
)
from bot.automod.normalize import normalize_text
from bot.tests.fakes import FakeChannel, FakeGuild, FakeMember, FakeRole


class FakeClock:
    """Injectable, controllable clock for stateful detectors."""

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def _guild() -> FakeGuild:
    return FakeGuild(10, owner_id=1)


def _member(guild: FakeGuild, user_id: int = 3) -> FakeMember:
    return FakeMember(user_id, "user", roles=[FakeRole(101, "user", 3)], guild=guild)


def _message(
    content: str,
    *,
    member: FakeMember | None = None,
    guild: FakeGuild | None = None,
    mentions: list | None = None,
    role_mentions: list | None = None,
    everyone: bool = False,
):
    guild = guild or _guild()
    member = member or _member(guild)
    channel = FakeChannel(20, guild=guild)
    return _build_message(
        content,
        guild=guild,
        member=member,
        channel=channel,
        mentions=mentions,
        role_mentions=role_mentions,
        everyone=everyone,
    )


def _build_message(
    content, *, guild, member, channel, mentions=None, role_mentions=None, everyone=False
):
    from bot.tests.fakes import FakeMessage

    return FakeMessage(
        1,
        content,
        guild=guild,
        author=member,
        channel=channel,
        mentions=mentions or [],
        role_mentions=role_mentions or [],
        mention_everyone=everyone,
    )


# ------------------------------------------------------------------- spam


def test_spam_detects_burst_within_window() -> None:
    clock = FakeClock()
    detector = SpamDetector(threshold=5, window_seconds=5, clock=clock)
    message = _message("hi")
    detection = None
    for _ in range(5):
        detection = detector.analyze(message, "hi")
        clock.advance(1)
    assert detection is not None
    assert detection.detector == "spam"
    assert "spam" in detection.reason


def test_spam_below_threshold_does_not_trigger() -> None:
    clock = FakeClock()
    detector = SpamDetector(threshold=5, window_seconds=5, clock=clock)
    message = _message("hi")
    for _ in range(4):
        assert detector.analyze(message, "hi") is None
        clock.advance(1)


def test_spam_spread_beyond_window_does_not_trigger() -> None:
    clock = FakeClock()
    detector = SpamDetector(threshold=5, window_seconds=5, clock=clock)
    message = _message("hi")
    detection = None
    for _ in range(5):
        detection = detector.analyze(message, "hi")
        clock.advance(2)  # 2s between messages: 5 messages span > 5s window
    assert detection is None


def test_spam_tracks_per_user_independently() -> None:
    clock = FakeClock()
    detector = SpamDetector(threshold=3, window_seconds=5, clock=clock)
    guild = _guild()
    first = _member(guild, 1)
    second = _member(guild, 2)
    for _ in range(3):
        detector.analyze(_message("a", member=first, guild=guild), "a")
        clock.advance(1)
    # The other user has sent nothing, so only the first user's key fires.
    assert detector.analyze(_message("b", member=second, guild=guild), "b") is None


def test_spam_history_is_bounded_per_user() -> None:
    clock = FakeClock()
    detector = SpamDetector(threshold=2, window_seconds=5, clock=clock)
    message = _message("hi")
    for _ in range(100):
        detector.analyze(message, "hi")
        clock.advance(1)
    # per_user for threshold=2 is min(max(2*2, 8), 200) = 8.
    assert len(detector._history.snapshot((message.guild.id, message.author.id))) <= 8


def test_spam_tracked_users_is_bounded() -> None:
    clock = FakeClock()
    detector = SpamDetector(threshold=5, window_seconds=5, max_users=3, clock=clock)
    guild = _guild()
    for user_id in range(20):
        member = _member(guild, user_id)
        detector.analyze(_message("hi", member=member, guild=guild), "hi")
        clock.advance(1)
    assert detector.tracked_users <= 3  # global cap enforced


# -------------------------------------------------------------- duplicate


def test_duplicate_detects_repeated_identical_messages() -> None:
    clock = FakeClock()
    detector = DuplicateDetector(threshold=4, window_seconds=30, clock=clock)
    message = _message("BUY NOW!!!")
    detection = None
    for _ in range(4):
        detection = detector.analyze(message, normalize_text(message.content))
        clock.advance(1)
    assert detection is not None
    assert detection.detector == "duplicate"


def test_duplicate_single_repetition_never_triggers() -> None:
    clock = FakeClock()
    detector = DuplicateDetector(threshold=4, window_seconds=30, clock=clock)
    message = _message("repeat me")
    for _ in range(2):
        assert detector.analyze(message, normalize_text(message.content)) is None
        clock.advance(1)


def test_duplicate_matches_normalized_variants() -> None:
    clock = FakeClock()
    detector = DuplicateDetector(threshold=3, window_seconds=30, clock=clock)
    # Different raw texts with the same normalized form count as duplicates.
    variants = ["BUY NOW!!!", "buy now", "Buy Now!"]
    for raw in variants:
        message = _message(raw)
        detection = detector.analyze(message, normalize_text(raw))
        clock.advance(1)
    assert detection is not None


def test_duplicate_different_messages_never_trigger() -> None:
    clock = FakeClock()
    detector = DuplicateDetector(threshold=4, window_seconds=30, clock=clock)
    for text in ["one", "two", "three", "four"]:
        message = _message(text)
        assert detector.analyze(message, normalize_text(text)) is None
        clock.advance(1)


def test_duplicate_stale_entries_expire() -> None:
    clock = FakeClock()
    detector = DuplicateDetector(threshold=3, window_seconds=5, clock=clock)
    message = _message("same")
    for _ in range(2):
        detector.analyze(message, normalize_text(message.content))
        clock.advance(6)  # each message is outside the window of the last
    assert detector.analyze(message, normalize_text(message.content)) is None


def test_duplicate_empty_normalized_skipped() -> None:
    clock = FakeClock()
    detector = DuplicateDetector(threshold=3, window_seconds=30, clock=clock)
    message = _message("!!!")
    assert detector.analyze(message, "") is None


def test_duplicate_tracked_users_is_bounded() -> None:
    clock = FakeClock()
    detector = DuplicateDetector(threshold=3, window_seconds=30, max_users=2, clock=clock)
    guild = _guild()
    for user_id in range(10):
        member = _member(guild, user_id)
        detector.analyze(_message("x", member=member, guild=guild), "x")
        clock.advance(1)
    assert detector.tracked_users <= 2


# -------------------------------------------------------------- mentions


def _mention_message(total_mentions: int = 0, *, roles: int = 0, everyone: bool = False, **kwargs):
    mentions = [object() for _ in range(total_mentions)]
    role_mentions = [object() for _ in range(roles)]
    return _message(
        "ping", mentions=mentions, role_mentions=role_mentions, everyone=everyone, **kwargs
    )


def test_mention_user_threshold() -> None:
    detector = MentionDetector(user_threshold=6)
    assert detector.analyze(_mention_message(5), "ping") is None
    assert detector.analyze(_mention_message(6), "ping") is not None


def test_mention_role_threshold() -> None:
    detector = MentionDetector(role_threshold=4)
    assert detector.analyze(_mention_message(roles=3), "ping") is None
    assert detector.analyze(_mention_message(roles=4), "ping") is not None


def test_mention_total_threshold_counts_all_dimensions() -> None:
    detector = MentionDetector(total_threshold=5)
    # 3 user mentions + 2 role mentions = 5 total.
    assert detector.analyze(_mention_message(3, roles=2), "ping") is not None


def test_mention_everyone_threshold_separate() -> None:
    detector = MentionDetector(everyone_threshold=1)
    assert detector.analyze(_mention_message(everyone=True), "ping") is not None
    assert detector.analyze(_mention_message(0), "ping") is None


def test_mention_zero_threshold_disables_dimension() -> None:
    detector = MentionDetector(user_threshold=0, total_threshold=0)
    assert detector.analyze(_mention_message(50), "ping") is None


def test_mention_negative_values_treated_as_disabled() -> None:
    detector = MentionDetector(user_threshold=-1)
    assert detector.analyze(_mention_message(50), "ping") is None


def test_mention_large_counts_trigger_without_blowing_up() -> None:
    detector = MentionDetector(user_threshold=10)
    assert detector.analyze(_mention_message(1_000), "ping") is not None


# ------------------------------------------------------------------- links


def test_link_allowlisted_domain_no_detection() -> None:
    detector = LinkDetector(allowed_domains=("youtube.com", "github.com", "discord.com"))
    assert detector.analyze(_message("watch https://youtube.com/watch?v=1"), "watch") is None
    assert detector.analyze(_message("code at https://github.com/org/repo"), "code") is None


def test_link_unlisted_url_detected() -> None:
    detector = LinkDetector(allowed_domains=("github.com",))
    detection = detector.analyze(_message("see https://evil.example.org/x"), "see")
    assert detection is not None
    assert detection.detector == "links"


def test_link_lookalike_domain_detected() -> None:
    detector = LinkDetector(allowed_domains=("github.com",))
    assert detector.analyze(_message("https://evilgithub.com/x"), "x") is not None
    assert detector.analyze(_message("https://github.com.evil.com/x"), "x") is not None


def test_link_subdomain_of_allowlist_is_fine() -> None:
    detector = LinkDetector(allowed_domains=("github.com",))
    assert detector.analyze(_message("https://api.github.com/v1"), "api") is None


def test_link_no_url_no_detection() -> None:
    detector = LinkDetector(allowed_domains=("youtube.com",))
    assert detector.analyze(_message("just text"), "justtext") is None


def test_link_empty_allowlist_blocks_all_urls() -> None:
    detector = LinkDetector(allowed_domains=())
    assert detector.analyze(_message("https://anything.example.org"), "x") is not None


# ----------------------------------------------------------------- invites


def test_invite_short_link_detected() -> None:
    detector = InviteDetector()
    assert detector.analyze(_message("join https://discord.gg/abc123"), "join") is not None
    assert detector.analyze(_message("discord.gg/xyz"), "x") is not None


def test_invite_canonical_paths_detected() -> None:
    detector = InviteDetector()
    assert detector.analyze(_message("https://discord.com/invite/abcDEF"), "x") is not None
    assert detector.analyze(_message("https://discordapp.com/invite/code_1"), "x") is not None


def test_invite_allowed_code_not_detected() -> None:
    detector = InviteDetector(allowed_codes=("welcome",))
    assert detector.analyze(_message("join https://discord.gg/welcome"), "join") is None
    # Case is normalized: the allowlist matches case-insensitively.
    assert detector.analyze(_message("join https://discord.gg/WELCOME"), "join") is None


def test_invite_other_code_detected_despite_allowlist() -> None:
    detector = InviteDetector(allowed_codes=("welcome",))
    assert detector.analyze(_message("join https://discord.gg/other"), "join") is not None


def test_invite_no_invite_no_detection() -> None:
    detector = InviteDetector()
    assert detector.analyze(_message("just text https://example.org"), "just text") is None
    assert detector.analyze(_message(""), "") is None


def test_invite_lookalike_not_detected() -> None:
    """A lookalike domain must not be treated as a Discord invite."""
    detector = InviteDetector()
    assert detector.analyze(_message("https://discord.gg.evil.com/x"), "x") is None
    assert detector.analyze(_message("https://notdiscord.com/invite/x"), "x") is None


# -------------------------------------------------------------------- raid


def _join(guild, user_id: int) -> FakeMember:
    return FakeMember(user_id, f"user{user_id}", guild=guild)


def test_raid_detects_join_burst_within_window() -> None:
    clock = FakeClock()
    detector = RaidDetector(threshold=5, window_seconds=10, clock=clock)
    guild = _guild()
    probe = type("Probe", (), {"guild": guild})()
    detection = None
    for i in range(5):
        detector.record_join(guild.id, i, clock())
        detection = detector.analyze(probe, "")
        clock.advance(1)
    assert detection is not None
    assert detection.detector == "raid"


def test_raid_below_threshold_no_detection() -> None:
    clock = FakeClock()
    detector = RaidDetector(threshold=5, window_seconds=10, clock=clock)
    guild = _guild()
    probe = type("Probe", (), {"guild": guild})()
    for i in range(4):
        detector.record_join(guild.id, i, clock())
        assert detector.analyze(probe, "") is None
        clock.advance(1)


def test_raid_spread_beyond_window_no_detection() -> None:
    clock = FakeClock()
    detector = RaidDetector(threshold=5, window_seconds=10, clock=clock)
    guild = _guild()
    probe = type("Probe", (), {"guild": guild})()
    for i in range(5):
        detector.record_join(guild.id, i, clock())
        detection = detector.analyze(probe, "")
        clock.advance(11)  # each join is outside the window of the previous
    assert detection is None


def test_raid_tracks_guilds_independently() -> None:
    clock = FakeClock()
    detector = RaidDetector(threshold=3, window_seconds=10, clock=clock)
    guild_a = _guild()
    guild_b = FakeGuild(20, owner_id=1)
    probe_a = type("Probe", (), {"guild": guild_a})()
    probe_b = type("Probe", (), {"guild": guild_b})()
    for i in range(3):
        detector.record_join(guild_a.id, i, clock())
        clock.advance(1)
    assert detector.analyze(probe_a, "") is not None
    assert detector.analyze(probe_b, "") is None  # guild B saw no joins


def test_raid_history_is_bounded() -> None:
    clock = FakeClock()
    detector = RaidDetector(threshold=2, window_seconds=5, max_guilds=2, max_joins=10, clock=clock)
    for guild_id in range(20):
        for i in range(3):
            detector.record_join(guild_id, i, clock())
    assert detector.tracked_guilds <= 2  # global cap enforced


def test_raid_recent_joiners_within_window() -> None:
    clock = FakeClock()
    detector = RaidDetector(threshold=2, window_seconds=10, clock=clock)
    guild = _guild()
    detector.record_join(guild.id, 1, clock())
    clock.advance(5)
    detector.record_join(guild.id, 2, clock())
    assert detector.recent_joiners(guild.id) == [1, 2]
    clock.advance(6)  # join 1 falls outside the window
    assert detector.recent_joiners(guild.id) == [2]


# ------------------------------------------------------------- word filter


def test_word_filter_whole_word_match() -> None:
    detector = WordFilterDetector(terms=("ass",))
    assert detector.analyze(_message("you are an ass"), "you are an ass") is not None
    # "assessment" must NOT match the term "ass" in whole-word mode.
    assert detector.analyze(_message("assessment"), "assessment") is None


def test_word_filter_case_insensitive() -> None:
    detector = WordFilterDetector(terms=("badword",))
    assert detector.analyze(_message("BADWORD"), "badword") is not None
    assert detector.analyze(_message("bAdWoRd"), "badword") is not None


def test_word_filter_punctuation_separators() -> None:
    detector = WordFilterDetector(terms=("fuck",))
    assert detector.analyze(_message("F.U.C.K!!!"), "fuck") is not None
    assert detector.analyze(_message("buy f.u.c.k now"), "buy fuck now") is not None


def test_word_filter_unicode_normalization() -> None:
    detector = WordFilterDetector(terms=("fuck",))
    assert detector.analyze(_message("ｆｕｃｋ"), "fuck") is not None


def test_word_filter_substring_mode_opt_in() -> None:
    whole_word = WordFilterDetector(terms=("ass",), substring=False)
    substring = WordFilterDetector(terms=("ass",), substring=True)
    assert whole_word.analyze(_message("assessment"), "assessment") is None
    assert substring.analyze(_message("assessment"), "assessment") is not None


def test_word_filter_no_terms_never_detects() -> None:
    detector = WordFilterDetector(terms=())
    assert detector.analyze(_message("anything"), "anything") is None


def test_word_filter_phrase_terms() -> None:
    detector = WordFilterDetector(terms=("kill yourself",))
    assert detector.analyze(_message("KILL YOURSELF!!!"), "kill yourself") is not None
    assert detector.analyze(_message("kill yourself slowly"), "kill yourself slowly") is not None
    assert detector.analyze(_message("killing"), "killing") is None


def test_word_filter_empty_normalized_skipped() -> None:
    detector = WordFilterDetector(terms=("ass",))
    assert detector.analyze(_message("!!!"), "") is None


def test_word_filter_large_message_bounded_work() -> None:
    detector = WordFilterDetector(terms=("needle",))
    haystack = "hay " * 5_000
    assert detector.analyze(_message(haystack), normalize_text(haystack)) is None
    assert (
        detector.analyze(_message(haystack + " NEEDLE"), normalize_text(haystack + " needle"))
        is not None
    )
