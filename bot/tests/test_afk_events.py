"""Tests for the message-driven AFK system.

The flow under test matches the product spec:

1. ``/afk`` or ``·afk`` sets the status and tags the nickname ``[AFK]``.
2. Mentioning an AFK member gets an automatic reply with the reason and how
   long ago they left.
3. The member's next message clears the status, restores the nickname, and
   posts a welcome back.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from bot.cogs.afk import afk_command
from bot.configuration.settings import Settings
from bot.core.bot import RiyxoenBot
from bot.database.config_repository import MemoryGuildConfigRepository
from bot.database.repository import MemoryCaseRepository
from bot.permissions.checks import PermissionChecker
from bot.prefix import PrefixDispatcher
from bot.services.afk import (
    AFK_NICK_PREFIX,
    AfkEventHandler,
    AfkService,
    build_afk_nickname,
    format_elapsed,
    is_afk_nickname,
    strip_afk_nickname,
)
from bot.services.cases import CaseService
from bot.services.guild_config import GuildConfigService
from bot.services.moderation import ModerationService
from bot.tests.fakes import (
    FakeChannel,
    FakeGuild,
    FakeInteraction,
    FakeMember,
    FakeMessage,
    FakePermissions,
    FakeRole,
)

GUILD_ID = 10
BOT_ID = 100_000
ALICE_ID = 7
BOB_ID = 8


class World:
    """A real ``RiyxoenBot`` wired to in-memory services and fake Discord objects.

    Using the real client means ``on_message`` itself is exercised, so the
    ordering between AFK events and prefix commands is covered, not just the
    individual pieces.
    """

    def __init__(self) -> None:
        self.settings = Settings(token="test-token", notify_users=False, command_prefix="·")
        self.bot = RiyxoenBot(self.settings)
        self.channel = FakeChannel(20, "general")
        bot_member = FakeMember(
            BOT_ID,
            "eclipse",
            roles=[FakeRole(900, "bot", 9)],
            guild_permissions=FakePermissions(moderate_members=True, manage_roles=True),
            bot=True,
        )
        self.guild = FakeGuild(
            GUILD_ID,
            owner_id=1,
            me=bot_member,
            members=[bot_member],
            channels=[self.channel],
        )
        self.channel.guild = self.guild

        self.case_service = CaseService(MemoryCaseRepository())
        self.config_service = GuildConfigService(
            MemoryGuildConfigRepository(), settings=self.settings
        )
        self.permissions = PermissionChecker(
            self.bot, self.settings, config_service=self.config_service
        )
        self.moderation_service = ModerationService(
            self.bot,
            self.case_service,
            settings=self.settings,
            permissions=self.permissions,
            config_service=self.config_service,
        )
        self.afk_service = AfkService()  # in-memory (no db_path)

        self.bot.config_service = self.config_service
        self.bot.case_service = self.case_service
        self.bot.moderation_service = self.moderation_service
        self.bot.permissions = self.permissions
        self.bot.afk_service = self.afk_service
        self.bot.prefix = PrefixDispatcher(self.bot, self.settings)
        self.bot.afk_events = AfkEventHandler(
            self.afk_service, status_command=self.bot.prefix.is_afk_command
        )
        self.bot.automod = None
        self._next_id = 1

    # ---------------------------------------------------------------- actors

    def member(self, user_id: int, name: str) -> FakeMember:
        member = FakeMember(
            user_id, name, roles=[FakeRole(500 + user_id, "user", 1)], guild=self.guild
        )
        self.guild.members.append(member)
        return member

    # --------------------------------------------------------------- driving

    async def send(self, author: FakeMember, content: str, mentions=None) -> None:
        message = FakeMessage(
            self._next_id,
            content,
            guild=self.guild,
            author=author,
            channel=self.channel,
            mentions=mentions,
        )
        self._next_id += 1
        await self.bot.on_message(message)

    def clear_messages(self) -> None:
        self.channel.sent_embeds.clear()
        self.channel.sent_messages.clear()

    @property
    def text(self) -> str:
        parts: list[str] = []
        for embed in self.channel.sent_embeds:
            if embed is None:
                continue
            parts.append(str(embed.title or ""))
            if embed.description:
                parts.append(str(embed.description))
            for field in embed.fields:
                parts.append(f"{field.name}: {field.value}")
        parts.extend(self.channel.sent_messages)
        return "\n".join(part for part in parts if part)


# ------------------------------------------------------------------- helpers


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (timedelta(seconds=0), "a moment"),
        (timedelta(seconds=12), "a moment"),
        (timedelta(seconds=30), "30 seconds"),
        (timedelta(minutes=1), "1 minute"),
        (timedelta(minutes=5), "5 minutes"),
        (timedelta(hours=2), "2 hours"),
        (timedelta(days=3), "3 days"),
        (timedelta(seconds=-60), "a moment"),
    ],
)
def test_format_elapsed(delta: timedelta, expected: str) -> None:
    assert format_elapsed(delta) == expected


def test_nickname_helpers_never_nest_tags() -> None:
    assert build_afk_nickname("alice") == f"{AFK_NICK_PREFIX}alice"
    assert is_afk_nickname("[AFK] alice") is True
    assert is_afk_nickname("alice") is False
    assert is_afk_nickname(None) is False
    assert strip_afk_nickname("[AFK] [AFK] alice") == "alice"
    # Discord caps nicknames at 32 characters.
    assert len(build_afk_nickname("x" * 40)) == 32


# ------------------------------------------------------------------ commands


async def test_prefix_afk_sets_status_and_tags_nickname() -> None:
    world = World()
    alice = world.member(ALICE_ID, "alice")

    await world.send(alice, "·afk studying")

    state = world.afk_service.get(GUILD_ID, alice.id)
    assert state is not None
    assert state.afk_message == "studying"
    assert alice.nick == f"{AFK_NICK_PREFIX}alice"
    assert "studying" in world.text


async def test_afk_command_does_not_clear_itself() -> None:
    """Re-running the AFK command updates the reason; activity clears it."""
    world = World()
    alice = world.member(ALICE_ID, "alice")

    await world.send(alice, "·afk studying")
    await world.send(alice, "·afk errands")

    state = world.afk_service.get(GUILD_ID, alice.id)
    assert state is not None
    assert state.afk_message == "errands"
    # No welcome-back message: the command is status management, not activity.
    assert "Welcome back" not in world.text


async def test_slash_afk_command_sets_status() -> None:
    world = World()
    alice = world.member(ALICE_ID, "alice")
    interaction = FakeInteraction(
        guild=world.guild, user=alice, client=world.bot, channel=world.channel
    )

    await afk_command.callback(interaction, "studying")

    state = world.afk_service.get(GUILD_ID, alice.id)
    assert state is not None
    assert state.afk_message == "studying"
    assert alice.nick == f"{AFK_NICK_PREFIX}alice"
    assert interaction.response.messages  # replied


# --------------------------------------------------------------- auto-answer


async def test_mention_gets_reason_and_elapsed_time() -> None:
    world = World()
    alice = world.member(ALICE_ID, "alice")
    bob = world.member(BOB_ID, "bob")

    await world.send(alice, "·afk studying")
    world.clear_messages()

    await world.send(bob, f"hey {alice.mention} are you there?", mentions=[alice])

    text = world.text
    assert "alice" in text
    assert "is AFK" in text
    assert "studying" in text
    assert "away for" in text


async def test_mention_without_reason_still_answers() -> None:
    world = World()
    alice = world.member(ALICE_ID, "alice")
    bob = world.member(BOB_ID, "bob")

    await world.send(alice, "·afk")
    world.clear_messages()

    await world.send(bob, f"ping {alice.mention}", mentions=[alice])

    text = world.text
    assert "is AFK" in text
    assert "away for" in text


async def test_no_answer_for_mentions_of_active_members() -> None:
    world = World()
    alice = world.member(ALICE_ID, "alice")
    bob = world.member(BOB_ID, "bob")

    await world.send(bob, f"hi {alice.mention}", mentions=[alice])

    assert "is AFK" not in world.text


# ------------------------------------------------------------------ removal


async def test_activity_clears_status_and_restores_nickname() -> None:
    world = World()
    alice = world.member(ALICE_ID, "alice")

    await world.send(alice, "·afk lunch")
    assert alice.nick == f"{AFK_NICK_PREFIX}alice"
    world.clear_messages()

    await world.send(alice, "back now")

    assert world.afk_service.get(GUILD_ID, alice.id) is None
    # The member had no nickname before, so it is cleared, not pinned.
    assert alice.nick is None
    assert "Welcome back" in world.text


async def test_activity_restores_a_previous_nickname() -> None:
    world = World()
    alice = world.member(ALICE_ID, "alice_user")
    alice.nick = "Ally"

    await world.send(alice, "·afk lunch")
    assert alice.nick == f"{AFK_NICK_PREFIX}Ally"
    world.clear_messages()

    await world.send(alice, "back")

    state = world.afk_service.get(GUILD_ID, alice.id)
    assert state is None
    assert alice.nick == "Ally"


async def test_activity_clears_afk_even_when_the_message_is_a_command() -> None:
    world = World()
    alice = world.member(ALICE_ID, "alice")

    await world.send(alice, "·afk lunch")
    world.clear_messages()

    await world.send(alice, "·help")

    assert world.afk_service.get(GUILD_ID, alice.id) is None
    assert "Welcome back" in world.text


async def test_bot_messages_and_dms_are_ignored() -> None:
    world = World()
    alice = world.member(ALICE_ID, "alice")
    await world.send(alice, "·afk lunch")
    world.clear_messages()

    robot = world.member(999, "robot")
    robot.bot = True
    await world.send(robot, "beep")
    assert world.afk_service.get(GUILD_ID, alice.id) is not None

    # A DM-shaped message (no guild) must be ignored without raising.
    dm = FakeMessage(
        99,
        "hello",
        guild=None,  # type: ignore[arg-type]
        author=alice,
        channel=world.channel,
    )
    await world.bot.afk_events.handle_message(dm)
    assert world.afk_service.get(GUILD_ID, alice.id) is not None


async def test_afk_survives_a_reopened_service(tmp_path) -> None:
    """AFK state is persisted, so a restart keeps the member AFK."""
    path = tmp_path / "afk.db"
    service = AfkService(path)
    service.set_afk(GUILD_ID, ALICE_ID, "alice", "sleeping")
    service.close()

    reopened = AfkService(path)
    try:
        state = reopened.get(GUILD_ID, ALICE_ID)
        assert state is not None
        assert state.afk_message == "sleeping"
        assert state.original_name == "alice"
    finally:
        reopened.close()
