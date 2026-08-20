"""Lightweight fakes for the Discord objects the service layer touches.

These deliberately avoid importing real discord.py state objects (which are
heavy to construct); they implement only the attributes and methods the code
under test reads. No real Discord server is ever contacted.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any


class FakeResponse:
    """Minimal stand-in for aiohttp responses used by discord exceptions.

    ``discord.errors.HTTPException.__init__`` reads ``response.status`` (and
    formats ``{0.status} {0.reason}``), so both attributes must exist.
    """

    def __init__(self, status: int = 403) -> None:
        self.status = status
        self.reason = "Fake"


def forbidden(message: str = "forbidden") -> Exception:
    """A real ``discord.Forbidden`` constructed with a fake response."""
    from discord import Forbidden

    return Forbidden(FakeResponse(403), message)


def not_found(message: str = "not found") -> Exception:
    """A real ``discord.NotFound`` constructed with a fake response."""
    from discord import NotFound

    return NotFound(FakeResponse(404), message)


class FakePermissions:
    """Stand-in for discord.Permissions; unknown flags default to False."""

    def __init__(self, **flags: bool) -> None:
        self._flags = dict(flags)

    def __getattr__(self, name: str) -> bool:
        return self._flags.get(name, False)


class FakePermissionOverwrite:
    """Stand-in for discord.PermissionOverwrite; unset flags default to None."""

    def __init__(self, **flags: bool | None) -> None:
        self._flags = dict(flags)

    def __getattr__(self, name: str) -> bool | None:
        return self._flags.get(name)


class FakeRole:
    def __init__(self, id: int, name: str = "role", position: int = 0) -> None:
        self.id = id
        self.name = name
        self.position = position
        self.colour: int | None = None
        self._edit_error: Exception | None = None

    @property
    def mention(self) -> str:
        return f"<@&{self.id}>"

    def fail_edit(self, error: Exception) -> None:
        self._edit_error = error

    async def edit(self, **kwargs: Any) -> None:
        if self._edit_error is not None:
            raise self._edit_error
        if "name" in kwargs:
            self.name = kwargs["name"]
        if "colour" in kwargs or "color" in kwargs:
            self.colour = kwargs.get("colour", kwargs.get("color"))


class FakeUser:
    def __init__(self, id: int, name: str = "user", bot: bool = False) -> None:
        self.id = id
        self.name = name
        self.bot = bot

    def __str__(self) -> str:
        return self.name

    @property
    def display_name(self) -> str:
        return self.name

    @property
    def mention(self) -> str:
        return f"<@{self.id}>"


class FakeMember(FakeUser):
    """Stand-in for discord.Member with configurable roles and failures."""

    def __init__(
        self,
        id: int,
        name: str = "member",
        roles: list[FakeRole] | None = None,
        guild: FakeGuild | None = None,
        guild_permissions: FakePermissions | None = None,
        bot: bool = False,
    ) -> None:
        super().__init__(id, name, bot=bot)
        self.roles = roles or []
        self.guild = guild
        self.guild_permissions = guild_permissions or FakePermissions()
        self.timed_out_until: datetime | None = None
        self.kicked = False
        self.banned = False
        self.sent: list[str] = []
        self._send_error: Exception | None = None

    @property
    def top_role(self) -> FakeRole:
        if not self.roles:
            return FakeRole(0, "@everyone", 0)
        return max(self.roles, key=lambda role: role.position)

    def fail_send(self, error: Exception) -> None:
        self._send_error = error

    async def timeout(self, until: datetime | timedelta, /, *, reason: str | None = None) -> None:
        # Real discord.py converts a timedelta into an absolute datetime.
        if isinstance(until, timedelta):
            until = datetime.now(UTC) + until
        self.timed_out_until = until
        self.timeout_reason = reason

    async def kick(self, *, reason: str | None = None) -> None:
        self.kicked = True
        self.kick_reason = reason

    async def ban(self, *, reason: str | None = None, **kwargs: Any) -> None:
        self.banned = True
        self.ban_reason = reason

    async def send(self, content: str, **kwargs: Any) -> None:
        if self._send_error is not None:
            raise self._send_error
        self.sent.append(content)


class FakeGuild:
    def __init__(
        self,
        id: int,
        name: str = "test guild",
        *,
        owner_id: int | None = None,
        me: FakeMember | None = None,
        members: list[FakeMember] | None = None,
        channels: list[FakeChannel] | None = None,
        roles: list[FakeRole] | None = None,
    ) -> None:
        self.id = id
        self.name = name
        self.owner_id = owner_id
        self.me = me
        self.members = members or []
        self.channels = channels or []
        self.roles = roles or []
        #: Users available to ``bans().flatten()`` (unban autocomplete tests).
        self.banned_users: list[Any] = []
        #: Stable @everyone role object (lock/unlock key off it).
        self.default_role = FakeRole(0, "@everyone", 0)
        self._unban_error: Exception | None = None
        self._action_error: Exception | None = None
        self._next_role_id = 1000

    def fail_action(self, error: Exception) -> None:
        """Make the next kick/ban raise ``error`` (simulates Discord rejection)."""
        self._action_error = error

    def get_member(self, user_id: int) -> FakeMember | None:
        return next((member for member in self.members if member.id == user_id), None)

    def get_role(self, role_id: int) -> FakeRole | None:
        return next((role for role in self.roles if role.id == role_id), None)

    async def create_role(
        self, *, name: str = "role", colour: int | None = None, **kwargs: Any
    ) -> FakeRole:
        # Discord places new roles below the creator's highest role, so the
        # managed role must sit below the bot's highest (position 9).
        role = FakeRole(self._next_role_id, name, position=5)
        self._next_role_id += 1
        role.colour = colour
        self.roles.append(role)
        return role

    def bans(self):
        """Return a fake ``AsyncIterator``-like object for ``await bans().flatten()``."""
        return _FakeBanIterator(self)

    async def fetch_ban(self, user: Any):
        raise not_found("not banned")

    async def fetch_member(self, user_id: int) -> FakeMember:
        member = self.get_member(user_id)
        if member is None:
            raise not_found("unknown member")
        return member

    def get_channel(self, channel_id: int) -> FakeChannel | None:
        return next((channel for channel in self.channels if channel.id == channel_id), None)

    def fail_unban(self, error: Exception) -> None:
        self._unban_error = error

    async def kick(self, user: Any, *, reason: str | None = None) -> None:
        if self._action_error is not None:
            raise self._action_error
        user.kicked = True

    async def ban(self, user: Any, *, reason: str | None = None, **kwargs: Any) -> None:
        if self._action_error is not None:
            raise self._action_error
        user.banned = True

    async def unban(self, user: Any, *, reason: str | None = None) -> None:
        if self._unban_error is not None:
            raise self._unban_error
        user.unbanned = True


class _FakeBan:
    """Stand-in for discord.BanEntry (``ban.user``)."""

    def __init__(self, user: FakeUser) -> None:
        self.user = user


class _FakeBanIterator:
    """Minimal stand-in for discord's ``AsyncIterator`` over ban entries."""

    def __init__(self, guild: FakeGuild) -> None:
        self.guild = guild

    async def flatten(self) -> list[Any]:
        return [_FakeBan(user) for user in getattr(self.guild, "banned_users", [])]


class FakeChannel:
    def __init__(
        self,
        id: int,
        name: str = "general",
        guild: FakeGuild | None = None,
        permissions_for_bot: FakePermissions | None = None,
    ) -> None:
        self.id = id
        self.name = name
        self.guild = guild
        self._permissions_for_bot = permissions_for_bot or FakePermissions(
            manage_messages=True, read_message_history=True, manage_channels=True
        )
        self.purged: list[int] = []
        self.sent_embeds: list[Any] = []
        self.sent_messages: list[str] = []
        self.slowmode_delay: int | None = None
        #: target -> FakePermissionOverwrite state (lock/unlock tests).
        self.overwrites: dict[Any, FakePermissionOverwrite] = {}
        self._set_permissions_calls: list[tuple[Any, dict]] = []

    @property
    def mention(self) -> str:
        return f"<#{self.id}>"

    def permissions_for(self, member: Any) -> FakePermissions:
        return self._permissions_for_bot

    def overwrites_for(self, target: Any) -> FakePermissionOverwrite:
        return self.overwrites.get(target, FakePermissionOverwrite())

    async def set_permissions(
        self,
        target: Any,
        *,
        overwrite: Any | None = None,
        reason: str | None = None,
        **permissions: bool | None,
    ) -> None:
        if overwrite is None and not permissions:
            # Deleting the overwrite entirely.
            self.overwrites.pop(target, None)
        else:
            current = self.overwrites.get(target, FakePermissionOverwrite())
            merged = FakePermissionOverwrite(**dict(current._flags))
            for key, value in permissions.items():
                setattr(merged, key, value)
            if overwrite is not None:
                for key, value in vars(overwrite).get("_flags", {}).items():
                    setattr(merged, key, value)
            self.overwrites[target] = merged
        self._set_permissions_calls.append((target, dict(permissions)))

    async def send(self, content: str | None = None, **kwargs: Any) -> None:
        self.sent_embeds.append(kwargs.get("embed"))
        self.sent_messages.append(content or "")

    async def reply(self, content: str | None = None, **kwargs: Any) -> None:
        await self.send(content, **kwargs)

    async def edit(self, **kwargs: Any) -> None:
        if "slowmode_delay" in kwargs:
            self.slowmode_delay = kwargs["slowmode_delay"]

    async def purge(self, limit: int | None = 100, **kwargs: Any) -> list[Any]:
        count = min(limit or 0, 50)
        self.purged.append(count)
        return list(range(count))


class FakeBot:
    """Stand-in for the discord.Client: identity + user lookup only."""

    def __init__(self, user_id: int = 100_000, user_name: str = "riyxoen") -> None:
        self.user = FakeUser(user_id, user_name, bot=True)
        self._users: dict[int, FakeUser] = {}
        self._intents: Any | None = None

    def add_user(self, user: FakeUser) -> None:
        self._users[user.id] = user

    @property
    def intents(self) -> Any:
        """Message-content availability for the prefix dispatcher."""
        if self._intents is None:
            return type("Intents", (), {"message_content": True})()
        return self._intents

    async def fetch_user(self, user_id: int) -> FakeUser:
        user = self._users.get(user_id)
        if user is None:
            raise not_found("unknown user")
        return user

    def get_user(self, user_id: int) -> FakeUser | None:
        return self._users.get(user_id)


class FakeFollowup:
    """Minimal stand-in for ``Interaction.followup``."""

    def __init__(self, response: FakeInteractionResponse) -> None:
        self._response = response

    async def send(self, content: str | None = None, **kwargs: Any) -> None:
        self._response.followup_messages.append(content or "")


class FakeInteractionResponse:
    """Minimal stand-in for ``Interaction.response``."""

    def __init__(self) -> None:
        self.messages: list[str] = []
        self.followup_messages: list[str] = []
        self.deferred = False
        self.followup = FakeFollowup(self)
        self.views: list[Any] = []

    def is_done(self) -> bool:
        return bool(self.messages) or self.deferred

    async def send_message(self, content: str | None = None, **kwargs: Any) -> None:
        self.messages.append(content or "")
        if kwargs.get("view") is not None:
            self.views.append(kwargs["view"])

    async def defer(self, **kwargs: Any) -> None:
        self.deferred = True


class FakeInteraction:
    """Stand-in for ``discord.Interaction`` used by command handlers."""

    def __init__(
        self,
        *,
        guild: FakeGuild,
        user: FakeMember,
        client: Any,
        channel: FakeChannel | None = None,
    ) -> None:
        self.guild = guild
        self.user = user
        self.client = client
        self.channel = channel
        self.response = FakeInteractionResponse()
        # Real ``discord.Interaction`` exposes ``followup`` at the top level.
        self.followup = self.response.followup


class FakeClient:
    """Stand-in for the bot client as command handlers see it."""

    def __init__(
        self,
        *,
        case_service: Any = None,
        permissions: Any = None,
        users: dict[int, FakeUser] | None = None,
        moderation_service: Any = None,
        confirmation_service: Any = None,
    ) -> None:
        self.case_service = case_service
        self.permissions = permissions
        self.moderation_service = moderation_service
        self.confirmation_service = confirmation_service
        self._users = users or {}

    def get_user(self, user_id: int) -> FakeUser | None:
        return self._users.get(user_id)


class FakeMessage:
    """Stand-in for ``discord.Message`` as the automod engine sees it."""

    def __init__(
        self,
        id: int,
        content: str,
        *,
        guild: FakeGuild,
        author: FakeMember,
        channel: FakeChannel,
        mentions: list[Any] | None = None,
        role_mentions: list[Any] | None = None,
        mention_everyone: bool = False,
    ) -> None:
        self.id = id
        self.content = content
        self.guild = guild
        self.author = author
        self.channel = channel
        self.mentions = mentions or []
        self.role_mentions = role_mentions or []
        self.mention_everyone = mention_everyone
        self.deleted = False
        self._delete_error: Exception | None = None

    def fail_delete(self, error: Exception) -> None:
        self._delete_error = error

    async def delete(self) -> None:
        if self._delete_error is not None:
            raise self._delete_error
        self.deleted = True
