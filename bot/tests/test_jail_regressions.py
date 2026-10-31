"""Regression tests for the critical jail/prefix and case-status bugs.

These cover the defects that the pre-existing suite could not see:

1. ``STATUS_CLEARED`` (``.clearwarnings``) was rejected by the SQLite CHECK
   constraint, so clearing warnings always failed.
2. ``·jail`` stripped the member's roles *before* the moderator permission
   check ran, so any member could destroy another member's roles.
3. ``·jail``/``·unjail`` went through the private ``ModerationService._punish``
   with a no-op ``asyncio.sleep(0)`` instead of a checked public path.
"""

from __future__ import annotations

import sqlite3

from bot.configuration.settings import Settings
from bot.database.config_repository import MemoryGuildConfigRepository
from bot.database.migrations import LATEST_SCHEMA_VERSION
from bot.database.repository import MemoryCaseRepository, SQLiteCaseRepository
from bot.moderation.cases import (
    STATUS_CLEARED,
    STATUS_FAILED,
    STATUS_SUCCESS,
    CaseRecord,
    utc_now,
)
from bot.permissions.checks import PermissionChecker
from bot.prefix import PrefixDispatcher
from bot.services.cases import CaseService
from bot.services.guild_config import GuildConfigService
from bot.services.jail import JailService
from bot.services.moderation import ModerationService
from bot.tests.fakes import (
    FakeBot,
    FakeChannel,
    FakeGuild,
    FakeMember,
    FakeMessage,
    FakePermissions,
    FakeRole,
)

GUILD_ID = 10
BOT_USER_ID = 100_000
TARGET_ID = 11


# ------------------------------------------------------------------- helpers


class _Intents:
    message_content = True


def _record(**overrides) -> CaseRecord:
    values = {
        "guild_id": GUILD_ID,
        "target_user_id": TARGET_ID,
        "moderator_user_id": 2,
        "action": "warn",
        "reason": "test",
        "created_at": utc_now(),
        "status": STATUS_SUCCESS,
    }
    values.update(overrides)
    return CaseRecord(**values)


class World:
    """Minimal in-memory bot wiring for the prefix dispatcher."""

    def __init__(self, case_repository=None) -> None:
        self.settings = Settings(token="test-token", notify_users=False, command_prefix="·")
        self.bot = FakeBot(BOT_USER_ID)
        self.bot._intents = _Intents()
        self.channel = FakeChannel(20, "general")
        bot_member = FakeMember(
            BOT_USER_ID,
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
        self.case_service = CaseService(case_repository or MemoryCaseRepository())
        self.config_service = GuildConfigService(
            MemoryGuildConfigRepository(), settings=self.settings
        )
        self.permissions = PermissionChecker(
            self.bot, self.settings, config_service=self.config_service
        )
        self.jail_service = JailService(self.permissions, self.config_service)
        self.moderation_service = ModerationService(
            self.bot,
            self.case_service,
            settings=self.settings,
            permissions=self.permissions,
            config_service=self.config_service,
        )
        self.bot.jail_service = self.jail_service
        self.bot.moderation_service = self.moderation_service
        self.bot.permissions = self.permissions
        self.bot.config_service = self.config_service
        self.bot.case_service = self.case_service
        self.dispatcher = PrefixDispatcher(self.bot, self.settings)
        self._next_message_id = 1

    # ---------------------------------------------------------------- actors

    def admin(self) -> FakeMember:
        return FakeMember(
            2,
            "admin",
            roles=[FakeRole(100, "admin", 8)],
            guild=self.guild,
            guild_permissions=FakePermissions(
                administrator=True, moderate_members=True, manage_roles=True
            ),
        )

    def moderator(self) -> FakeMember:
        return FakeMember(
            3,
            "mod",
            roles=[FakeRole(101, "mod", 7)],
            guild=self.guild,
            guild_permissions=FakePermissions(moderate_members=True, manage_roles=True),
        )

    def regular(self) -> FakeMember:
        return FakeMember(4, "member", roles=[FakeRole(50, "user", 1)], guild=self.guild)

    def target(self, *, user_id: int = TARGET_ID, position: int = 99) -> FakeMember:
        """A plain member with two normal roles (registered on the guild)."""
        user_role = FakeRole(600, "user", 2)
        trusted_role = FakeRole(700, "trusted", 3)
        self.guild.roles.extend([user_role, trusted_role])
        member = FakeMember(
            user_id, "target", roles=[user_role, trusted_role], guild=self.guild
        )
        self.guild.members.append(member)
        return member

    # --------------------------------------------------------------- driving

    async def send(self, author: FakeMember, content: str) -> None:
        message = FakeMessage(
            self._next_message_id,
            content,
            guild=self.guild,
            author=author,
            channel=self.channel,
        )
        self._next_message_id += 1
        await self.dispatcher.handle(message)

    @property
    def replies(self) -> str:
        """Every text the dispatcher posted (embed titles/descriptions/fields)."""
        parts: list[str] = []
        for embed in self.channel.sent_embeds:
            if embed is None:
                continue
            parts.append(f"{embed.title}")
            if embed.description:
                parts.append(str(embed.description))
            for field in embed.fields:
                parts.append(f"{field.name}: {field.value}")
        parts.extend(self.channel.sent_messages)
        return "\n".join(part for part in parts if part)

    def jail_role(self) -> FakeRole | None:
        config = self.config_service.get(GUILD_ID)
        if config.jail_role_id is None:
            return None
        return self.guild.get_role(config.jail_role_id)


# ----------------------------------------------------------- cleared status


def test_sqlite_accepts_cleared_case_status(tmp_path) -> None:
    """``.clearwarnings`` writes 'cleared', which SQLite must accept."""
    repo = SQLiteCaseRepository(tmp_path / "cases.db")
    try:
        stored = repo.create(_record())
        updated = repo.update_status(GUILD_ID, stored.case_id, STATUS_CLEARED)

        assert updated is not None
        assert updated.status == STATUS_CLEARED
        # And it must survive a reopen (i.e. it was really persisted).
        reread = repo.get(GUILD_ID, stored.case_id)
        assert reread is not None
        assert reread.status == STATUS_CLEARED
    finally:
        repo.close()


def test_sqlite_schema_is_latest_version(tmp_path) -> None:
    """A fresh database is migrated to the newest schema version."""
    path = tmp_path / "cases.db"
    repo = SQLiteCaseRepository(path)
    repo.close()

    connection = sqlite3.connect(path)
    try:
        (version,) = connection.execute("PRAGMA user_version").fetchone()
    finally:
        connection.close()
    assert version == LATEST_SCHEMA_VERSION


def test_v6_rebuild_preserves_case_ids_and_numbering(tmp_path) -> None:
    """Re-running the table rebuild keeps existing IDs and keeps counting up."""
    path = tmp_path / "cases.db"
    repo = SQLiteCaseRepository(path)
    first = repo.create(_record())
    second = repo.create(_record())
    repo.close()

    # Pretend the database predates the rebuild.
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA user_version = 5")
        connection.commit()
    finally:
        connection.close()

    repo = SQLiteCaseRepository(path)
    try:
        assert repo.get(GUILD_ID, first.case_id) is not None
        assert repo.get(GUILD_ID, second.case_id) is not None
        third = repo.create(_record())
        assert third.case_id == second.case_id + 1
    finally:
        repo.close()


# --------------------------------------------------------- jail permission


async def test_denied_jail_never_strips_roles() -> None:
    """A member without moderator rights cannot strip another member's roles."""
    world = World()
    target = world.target()
    original_ids = {role.id for role in target.roles}

    await world.send(world.admin(), "·jail setup")
    await world.send(world.regular(), f"·jail {target.mention} spam")

    jail_role = world.jail_role()
    assert jail_role is not None
    assert {role.id for role in target.roles} == original_ids
    assert jail_role not in target.roles
    assert "permission" in world.replies.lower()

    cases = world.case_service.list_for_guild(GUILD_ID)
    failed = [case for case in cases.items if case.action == "jail"]
    assert failed, "a denied jail must still be recorded"
    assert all(case.status == STATUS_FAILED for case in failed)


async def test_jail_hierarchy_failure_leaves_roles_intact() -> None:
    """A hierarchy rejection must not touch the target's roles."""
    world = World()
    powerful = FakeMember(77, "boss", roles=[FakeRole(999, "boss", 99)], guild=world.guild)
    world.guild.members.append(powerful)
    original_ids = {role.id for role in powerful.roles}

    await world.send(world.admin(), "·jail setup")
    await world.send(world.admin(), f"·jail {powerful.mention} spam")

    assert {role.id for role in powerful.roles} == original_ids
    cases = world.case_service.list_for_guild(GUILD_ID)
    failed = [case for case in cases.items if case.action == "jail"]
    assert failed and all(case.status == STATUS_FAILED for case in failed)


# ------------------------------------------------------------- jail cycle


async def test_jail_stores_previous_roles_and_unjail_restores_them() -> None:
    """A full jail/unjail cycle preserves and restores the member's roles."""
    world = World()
    target = world.target()

    await world.send(world.admin(), "·jail setup")
    jail_role = world.jail_role()
    assert jail_role is not None

    await world.send(world.moderator(), f"·jail {target.mention} spam")

    assert jail_role in target.roles
    assert {role.id for role in target.roles} == {jail_role.id}

    jail_case = [
        case
        for case in world.case_service.list_for_guild(GUILD_ID).items
        if case.action == "jail"
    ][0]
    assert jail_case.status == STATUS_SUCCESS
    assert jail_case.metadata is not None
    assert set(jail_case.metadata["previous_role_ids"]) == {600, 700}

    await world.send(world.moderator(), f"·unjail {target.mention}")

    assert jail_role not in target.roles
    assert {role.id for role in target.roles} == {600, 700}

    unjail_case = [
        case
        for case in world.case_service.list_for_guild(GUILD_ID).items
        if case.action == "unjail"
    ][0]
    assert unjail_case.status == STATUS_SUCCESS
    assert unjail_case.metadata is not None
    assert unjail_case.metadata["restored_role_count"] == 2


async def test_unjail_without_jail_case_still_releases_the_member() -> None:
    """A member jailed outside the bot can still be released."""
    world = World()
    target = world.target()

    await world.send(world.admin(), "·jail setup")
    jail_role = world.jail_role()
    assert jail_role is not None
    await target.add_roles(jail_role)  # jail applied by hand: no case metadata

    await world.send(world.moderator(), f"·unjail {target.mention}")

    assert jail_role not in target.roles
    assert {role.id for role in target.roles} == {600, 700}


async def test_unjail_refuses_when_not_jailed() -> None:
    """Unjailing someone who is not jailed is a safe, recorded refusal."""
    world = World()
    target = world.target()

    await world.send(world.admin(), "·jail setup")
    await world.send(world.moderator(), f"·unjail {target.mention}")

    assert "isn't currently jailed" in world.replies.lower()
    cases = world.case_service.list_for_guild(GUILD_ID)
    unjail = [case for case in cases.items if case.action == "unjail"]
    assert unjail and all(case.status == STATUS_FAILED for case in unjail)


async def test_jail_is_idempotent_for_already_jailed_member() -> None:
    """Jailing twice is refused and the roles are not stripped twice."""
    world = World()
    target = world.target()

    await world.send(world.admin(), "·jail setup")
    await world.send(world.moderator(), f"·jail {target.mention} spam")
    await world.send(world.moderator(), f"·jail {target.mention} spam again")

    assert "already jailed" in world.replies.lower()
    jail_role = world.jail_role()
    assert jail_role is not None and jail_role in target.roles
