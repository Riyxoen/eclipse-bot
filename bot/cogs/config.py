"""Server administration commands: ``/config`` (Phase 5).

Architecture per the spec:

    Discord command -> admin permission layer -> configuration service ->
    local SQLite -> moderation engine

The cog stays thin: every command verifies the caller is an administrator
through the shared :class:`bot.permissions.checks.PermissionChecker` (never
trusting the command's visible permission gate), then delegates to the
:class:`bot.services.guild_config.GuildConfigService`. No SQL and no
configuration logic live here; invalid values are rejected by the service's
validators with safe messages.

Permission model: changing or viewing server configuration requires the
server owner, the Discord ``administrator`` permission, or a configured
administrator role. A regular moderator never receives it automatically.
Every successful change emits an audit log line (service) and a summary
embed to the guild's log channel when mod logs are enabled (cog).
"""

from __future__ import annotations

import logging
from typing import Any, Literal

import discord
from discord import app_commands

from bot.configuration.display import format_config_view
from bot.configuration.errors import GuildConfigError
from bot.moderation.errors import ModerationError
from bot.services.guild_config import GuildConfigService
from bot.services.moderation import ModerationService

logger = logging.getLogger("riyxoen.config_cog")

_ACTION_DESCRIPTION = "Action to take when the detector fires (delete, warn, or timeout)."
_LINK_ACTION_DESCRIPTION = "Link handling (allow, delete, warn, or timeout)."


def _parse_id(raw: str, name: str) -> int:
    """Parse a snowflake ID string; raises a safe error when invalid."""
    cleaned = raw.strip()
    if not cleaned.isdigit() or int(cleaned) <= 0:
        raise GuildConfigError(f"{name} must be a positive ID number.")
    return int(cleaned)


class ConfigCog(app_commands.Group):
    """Server configuration commands. Commands call the service; no logic here."""

    # Nested subgroups (discord.py collects Group instances from the class
    # namespace as children of the cog).
    moderation = app_commands.Group(name="moderation", description="Moderation engine settings.")
    roles = app_commands.Group(
        name="roles", description="Configure moderator and administrator roles."
    )
    exemptions = app_commands.Group(
        name="exemptions", description="Configure automated-moderation exemptions."
    )

    def __init__(
        self,
        bot: discord.Client,
        config_service: GuildConfigService,
        moderation_service: ModerationService,
    ) -> None:
        super().__init__(name="config", description="Server configuration (administrators).")
        self.bot = bot
        self.config_service = config_service
        self.moderation_service = moderation_service

    # -------------------------------------------------------------- helpers

    def _require_admin(self, interaction: discord.Interaction) -> None:
        """Gate config behind the shared admin permission layer (server-side)."""
        permissions = interaction.client.permissions
        permissions.require_guild(interaction.guild)
        permissions.require_administrator(interaction.user, interaction.guild)

    async def _deny(
        self, interaction: discord.Interaction, exc: ModerationError | GuildConfigError
    ) -> None:
        """Show a safe denial/validation message; detail is in the logs."""
        logger.info("config denied: %s (user=%s)", exc.user_message, interaction.user.id)
        await interaction.response.send_message(exc.user_message, ephemeral=True)

    async def _apply(
        self,
        interaction: discord.Interaction,
        changes: dict,
        *,
        summary: str,
    ) -> None:
        """Validate via the service, respond with old -> new, and announce."""
        try:
            self._require_admin(interaction)
        except ModerationError as exc:
            await self._deny(interaction, exc)
            return
        try:
            previous = self.config_service.get(interaction.guild.id)
            self.config_service.update(
                interaction.guild.id,
                actor_user_id=interaction.user.id,
                changes=changes,
            )
        except GuildConfigError as exc:
            await self._deny(interaction, exc)
            return
        lines = [f"Updated **{summary}**."]
        for key, value in changes.items():
            lines.append(f"- `{key}`: `{getattr(previous, key)}` → `{value}`")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)
        await self._announce_change(interaction, summary, list(changes))

    async def _announce_change(
        self,
        interaction: discord.Interaction,
        summary: str,
        keys: list[str],
    ) -> None:
        """Best-effort summary embed to the guild's log channel (if enabled)."""
        actor = (
            getattr(interaction.user, "mention", None)
            or f"<@{getattr(interaction.user, 'id', '?')}>"
        )
        await self.moderation_service.post_event(
            interaction.guild,
            title="Configuration change",
            fields=[
                ("Administrator", actor),
                ("Change", summary),
                ("Settings", ", ".join(keys) if keys else "reset"),
            ],
        )

    # ----------------------------------------------------------------- view

    @app_commands.command(name="view", description="Show this server's configuration.")
    @app_commands.guild_only()
    @app_commands.describe(page="Page number (default 1).")
    async def view(self, interaction: discord.Interaction, page: int = 1) -> None:
        """Show the guild's configuration (paginated, admin only)."""
        try:
            self._require_admin(interaction)
        except ModerationError as exc:
            await self._deny(interaction, exc)
            return
        config = self.config_service.get(interaction.guild.id)
        await interaction.response.send_message(
            format_config_view(config, interaction.guild, page=page),
            ephemeral=True,
        )

    # ----------------------------------------------------------- moderation

    @moderation.command(name="automod", description="Enable or disable automated moderation here.")
    async def moderation_automod(self, interaction: discord.Interaction, enabled: bool) -> None:
        """Toggle the per-guild automod master switch."""
        await self._apply(interaction, {"automod_enabled": enabled}, summary="automated moderation")

    @moderation.command(name="spam", description="Configure spam detection.")
    @app_commands.describe(
        threshold="Messages within the window (2-1000).",
        window_seconds="Time window in seconds (1-86400).",
        action=_ACTION_DESCRIPTION,
    )
    async def moderation_spam(
        self,
        interaction: discord.Interaction,
        threshold: int,
        window_seconds: int,
        action: Literal["delete", "warn", "timeout"] | None = None,
    ) -> None:
        """Set the spam threshold, window, and action."""
        changes: dict = {"spam_threshold": threshold, "spam_window_seconds": window_seconds}
        if action is not None:
            changes["spam_action"] = action
        await self._apply(interaction, changes, summary="spam detection")

    @moderation.command(name="duplicate", description="Configure duplicate-message detection.")
    @app_commands.describe(
        threshold="Identical messages within the window (2-1000).",
        window_seconds="Time window in seconds (1-86400).",
        action=_ACTION_DESCRIPTION,
    )
    async def moderation_duplicate(
        self,
        interaction: discord.Interaction,
        threshold: int,
        window_seconds: int,
        action: Literal["delete", "warn", "timeout"] | None = None,
    ) -> None:
        """Set the duplicate threshold, window, and action."""
        changes: dict = {
            "duplicate_threshold": threshold,
            "duplicate_window_seconds": window_seconds,
        }
        if action is not None:
            changes["duplicate_action"] = action
        await self._apply(interaction, changes, summary="duplicate detection")

    @moderation.command(name="mentions", description="Configure mention-spam thresholds.")
    @app_commands.describe(
        user_threshold="Max user mentions before action (0 disables).",
        role_threshold="Max role mentions before action (0 disables).",
        total_threshold="Max total mentions before action (0 disables).",
        action=_ACTION_DESCRIPTION,
    )
    async def moderation_mentions(
        self,
        interaction: discord.Interaction,
        user_threshold: int | None = None,
        role_threshold: int | None = None,
        total_threshold: int | None = None,
        action: Literal["delete", "warn", "timeout"] | None = None,
    ) -> None:
        """Set mention thresholds (only the provided dimensions change)."""
        changes: dict = {}
        if user_threshold is not None:
            changes["mention_user_threshold"] = user_threshold
        if role_threshold is not None:
            changes["mention_role_threshold"] = role_threshold
        if total_threshold is not None:
            changes["mention_total_threshold"] = total_threshold
        if action is not None:
            changes["mention_action"] = action
        if not changes:
            await self._deny(
                interaction,
                GuildConfigError("Provide at least one threshold or action to change."),
            )
            return
        await self._apply(interaction, changes, summary="mention detection")

    @moderation.command(name="links", description="Configure link filtering.")
    @app_commands.describe(action=_LINK_ACTION_DESCRIPTION)
    async def moderation_links(
        self,
        interaction: discord.Interaction,
        action: Literal["allow", "delete", "warn", "timeout"],
    ) -> None:
        """Set the link action (allowed domains are managed via /config moderation domains)."""
        await self._apply(interaction, {"link_action": action}, summary="link filtering")

    @moderation.command(name="words", description="Manage blocked terms.")
    @app_commands.describe(
        sub="add, remove, or list blocked terms.",
        term="The term to add/remove (required for add/remove).",
    )
    async def moderation_words(
        self,
        interaction: discord.Interaction,
        sub: Literal["add", "remove", "list"],
        term: str | None = None,
    ) -> None:
        """Add, remove, or list the word filter's blocked terms."""
        await self._manage_terms(
            interaction,
            "blocked term",
            "blocked_terms",
            sub,
            term,
            lambda guild_id, actor_user_id, value: self.config_service.add_blocked_term(
                guild_id, actor_user_id=actor_user_id, term=value
            ),
            lambda guild_id, actor_user_id, value: self.config_service.remove_blocked_term(
                guild_id, actor_user_id=actor_user_id, term=value
            ),
            lambda config: config.blocked_terms,
        )

    @moderation.command(name="domains", description="Manage allowed link domains.")
    @app_commands.describe(
        sub="add, remove, or list allowed domains.",
        domain="The domain to add/remove (required for add/remove).",
    )
    async def moderation_domains(
        self,
        interaction: discord.Interaction,
        sub: Literal["add", "remove", "list"],
        domain: str | None = None,
    ) -> None:
        """Add, remove, or list allowed link domains."""
        await self._manage_terms(
            interaction,
            "domain",
            "allowed_domains",
            sub,
            domain,
            lambda guild_id, actor_user_id, value: self.config_service.add_allowed_domain(
                guild_id, actor_user_id=actor_user_id, domain=value
            ),
            lambda guild_id, actor_user_id, value: self.config_service.remove_allowed_domain(
                guild_id, actor_user_id=actor_user_id, domain=value
            ),
            lambda config: config.allowed_domains,
        )

    async def _manage_terms(
        self,
        interaction: discord.Interaction,
        label: str,
        field: str,
        sub: str,
        value: str | None,
        add_fn,
        remove_fn,
        getter,
    ) -> None:
        try:
            self._require_admin(interaction)
        except ModerationError as exc:
            await self._deny(interaction, exc)
            return
        if sub == "list":
            config = self.config_service.get(interaction.guild.id)
            entries = getter(config)
            text = ", ".join(entries) if entries else "none"
            await interaction.response.send_message(
                f"Configured {label}s: `{text}`", ephemeral=True
            )
            return
        if value is None or not value.strip():
            await self._deny(
                interaction, GuildConfigError(f"A {label} value is required for that action.")
            )
            return
        try:
            if sub == "add":
                add_fn(interaction.guild.id, actor_user_id=interaction.user.id, value=value)
            else:
                remove_fn(interaction.guild.id, actor_user_id=interaction.user.id, value=value)
        except GuildConfigError as exc:
            await self._deny(interaction, exc)
            return
        verb = "Added" if sub == "add" else "Removed"
        await interaction.response.send_message(
            f"{verb} {label}: `{value.strip()}`", ephemeral=True
        )
        await self._announce_change(interaction, f"{label} {sub}", [field])

    @moderation.command(
        name="escalation", description="Set warning escalation (count:seconds pairs)."
    )
    @app_commands.describe(
        spec="Pairs like 3:3600,5:43200 (3rd warning -> 1h timeout); empty clears escalation."
    )
    async def moderation_escalation(self, interaction: discord.Interaction, spec: str) -> None:
        """Set the warning-escalation policy (empty string disables it)."""
        await self._apply(interaction, {"escalation": spec.strip()}, summary="warning escalation")

    @moderation.command(name="cooldown", description="Set the enforcement cooldown in seconds.")
    @app_commands.describe(seconds="Seconds between enforcements for the same user (0 disables).")
    async def moderation_cooldown(self, interaction: discord.Interaction, seconds: int) -> None:
        """Set the automated-enforcement cooldown."""
        await self._apply(
            interaction, {"enforcement_cooldown_seconds": seconds}, summary="enforcement cooldown"
        )

    @moderation.command(name="timeout-duration", description="Set the default timeout duration.")
    @app_commands.describe(seconds="Seconds for detector timeouts (1 to 28 days).")
    async def moderation_timeout_duration(
        self, interaction: discord.Interaction, seconds: int
    ) -> None:
        """Set the default timeout duration used by the 'timeout' action."""
        await self._apply(
            interaction, {"timeout_duration_seconds": seconds}, summary="default timeout duration"
        )

    @moderation.command(name="purge-max", description="Set the maximum purge amount.")
    @app_commands.describe(amount="Maximum messages a single purge may delete (1-1000).")
    async def moderation_purge_max(self, interaction: discord.Interaction, amount: int) -> None:
        """Set the guild's maximum purge amount."""
        await self._apply(interaction, {"max_purge_amount": amount}, summary="maximum purge amount")

    @moderation.command(name="dm", description="Enable or disable DM notifications.")
    @app_commands.describe(enabled="Whether punished users receive a DM.")
    async def moderation_dm(self, interaction: discord.Interaction, enabled: bool) -> None:
        """Toggle DM notifications for punished users."""
        await self._apply(interaction, {"notify_users": enabled}, summary="DM notifications")

    # ----------------------------------------------------------------- prefix

    @app_commands.command(name="prefix", description="Set the text-command prefix for this server.")
    @app_commands.guild_only()
    @app_commands.describe(prefix="1-3 non-space characters, e.g. . or !")
    async def set_prefix(self, interaction: discord.Interaction, prefix: str) -> None:
        """Set the per-guild text-command prefix (Phase 10).

        The prefix dispatcher reads the guild's configured prefix per message,
        so the change applies to the next command immediately.
        """
        await self._apply(interaction, {"command_prefix": prefix.strip()}, summary="command prefix")

    # ----------------------------------------------------------------- logs

    @app_commands.command(
        name="logs", description="Set the moderation log channel and toggle logs."
    )
    @app_commands.guild_only()
    @app_commands.describe(
        channel="The channel to receive log embeds (optional).",
        enabled="Whether moderation logs are posted (optional).",
    )
    async def logs(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
        enabled: bool | None = None,
    ) -> None:
        """Set the log channel and/or toggle moderation logs."""
        if channel is None and enabled is None:
            await self._deny(
                interaction,
                GuildConfigError("Provide a channel and/or an enabled value."),
            )
            return
        try:
            self._require_admin(interaction)
        except ModerationError as exc:
            await self._deny(interaction, exc)
            return
        changes: dict = {}
        warning: str | None = None
        if channel is not None:
            if (
                getattr(channel, "guild", None) is not None
                and channel.guild.id != interaction.guild.id
            ):
                await self._deny(
                    interaction, GuildConfigError("The log channel must be in this server.")
                )
                return
            changes["log_channel_id"] = channel.id
            # Setting a channel enables log posts unless the caller also
            # chose an explicit enabled value.
            if enabled is None:
                changes["mod_log_enabled"] = True
            bot_member = getattr(interaction.guild, "me", None)
            if bot_member is not None:
                perms = channel.permissions_for(bot_member)
                if not getattr(perms, "send_messages", False) or not getattr(
                    perms, "embed_links", False
                ):
                    warning = (
                        "The bot can't send embeds in that channel (missing Send Messages / "
                        "Embed Links). The channel was saved anyway — fix the channel's "
                        "permissions for log posts to work."
                    )
        if enabled is not None:
            changes["mod_log_enabled"] = enabled
        try:
            previous = self.config_service.get(interaction.guild.id)
            self.config_service.update(
                interaction.guild.id, actor_user_id=interaction.user.id, changes=changes
            )
        except GuildConfigError as exc:
            await self._deny(interaction, exc)
            return
        lines = ["Updated **moderation logs**."]
        for key, value in changes.items():
            lines.append(f"- `{key}`: `{getattr(previous, key)}` → `{value}`")
        if warning:
            lines.append(f"⚠ {warning}")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)
        await self._announce_change(interaction, "moderation logs", list(changes))

    # ---------------------------------------------------------------- roles

    @roles.command(name="moderator-add", description="Add a moderator role.")
    async def roles_moderator_add(
        self, interaction: discord.Interaction, role: discord.Role
    ) -> None:
        """Grant a role the ability to run every moderation command."""
        await self._role_change(interaction, "moderator", role, add=True)

    @roles.command(name="moderator-remove", description="Remove a moderator role by ID.")
    async def roles_moderator_remove(self, interaction: discord.Interaction, role_id: str) -> None:
        """Revoke a moderator role (works even if the role was deleted)."""
        await self._role_change(interaction, "moderator", role_id, add=False)

    @roles.command(name="administrator-add", description="Add an administrator role.")
    async def roles_administrator_add(
        self, interaction: discord.Interaction, role: discord.Role
    ) -> None:
        """Grant a role the ability to change server configuration."""
        await self._role_change(interaction, "administrator", role, add=True)

    @roles.command(name="administrator-remove", description="Remove an administrator role by ID.")
    async def roles_administrator_remove(
        self, interaction: discord.Interaction, role_id: str
    ) -> None:
        """Revoke an administrator role (works even if the role was deleted)."""
        await self._role_change(interaction, "administrator", role_id, add=False)

    async def _role_change(
        self,
        interaction: discord.Interaction,
        kind: str,
        target: discord.Role | str,
        *,
        add: bool,
    ) -> None:
        try:
            self._require_admin(interaction)
        except ModerationError as exc:
            await self._deny(interaction, exc)
            return
        try:
            if hasattr(target, "id") and hasattr(target, "mention"):
                # A role object (real discord.Role or a test fake).
                if (
                    getattr(target, "guild", None) is not None
                    and target.guild.id != interaction.guild.id
                ):
                    await self._deny(
                        interaction, GuildConfigError("The role must be in this server.")
                    )
                    return
                role_id: int = target.id
                label = getattr(target, "mention", f"<@&{target.id}>")
            else:
                role_id = _parse_id(target, "role ID")
                label = self._role_label(interaction.guild, role_id)
            if add:
                updated = self.config_service.add_role(
                    interaction.guild.id,
                    actor_user_id=interaction.user.id,
                    kind=kind,
                    role_id=role_id,
                )
            else:
                updated = self.config_service.remove_role(
                    interaction.guild.id,
                    actor_user_id=interaction.user.id,
                    kind=kind,
                    role_id=role_id,
                )
        except GuildConfigError as exc:
            await self._deny(interaction, exc)
            return
        field = f"{kind}_role_ids"
        count = len(getattr(updated, field))
        verb = "Added" if add else "Removed"
        await interaction.response.send_message(
            f"{verb} {kind} role {label} ({count} configured).", ephemeral=True
        )
        await self._announce_change(interaction, f"{kind} role {verb.lower()}", [field])

    @staticmethod
    def _role_label(guild: discord.Guild, role_id: int) -> str:
        role = guild.get_role(role_id)
        if role is not None:
            return role.mention
        return f"<@&{role_id}>"

    # ------------------------------------------------------------ exemptions

    @exemptions.command(name="user-add", description="Exempt a user from automated moderation.")
    async def exemptions_user_add(
        self, interaction: discord.Interaction, member: discord.Member
    ) -> None:
        """Exempt a member by ID."""
        await self._exemption_change(interaction, "user", member, add=True)

    @exemptions.command(name="user-remove", description="Remove a user exemption by ID.")
    async def exemptions_user_remove(self, interaction: discord.Interaction, user_id: str) -> None:
        """Remove a user exemption (works even if they left)."""
        await self._exemption_change(interaction, "user", user_id, add=False)

    @exemptions.command(name="role-add", description="Exempt a role from automated moderation.")
    async def exemptions_role_add(
        self, interaction: discord.Interaction, role: discord.Role
    ) -> None:
        """Exempt a role by ID."""
        await self._exemption_change(interaction, "role", role, add=True)

    @exemptions.command(name="role-remove", description="Remove a role exemption by ID.")
    async def exemptions_role_remove(self, interaction: discord.Interaction, role_id: str) -> None:
        """Remove a role exemption (works even if the role was deleted)."""
        await self._exemption_change(interaction, "role", role_id, add=False)

    @exemptions.command(
        name="channel-add", description="Exempt a channel from automated moderation."
    )
    async def exemptions_channel_add(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        """Exempt a channel by ID."""
        await self._exemption_change(interaction, "channel", channel, add=True)

    @exemptions.command(name="channel-remove", description="Remove a channel exemption by ID.")
    async def exemptions_channel_remove(
        self, interaction: discord.Interaction, channel_id: str
    ) -> None:
        """Remove a channel exemption (works even if the channel was deleted)."""
        await self._exemption_change(interaction, "channel", channel_id, add=False)

    async def _exemption_change(
        self,
        interaction: discord.Interaction,
        kind: str,
        target: Any,
        *,
        add: bool,
    ) -> None:
        try:
            self._require_admin(interaction)
        except ModerationError as exc:
            await self._deny(interaction, exc)
            return
        try:
            if hasattr(target, "id") and hasattr(target, "mention"):
                if (
                    getattr(target, "guild", None) is not None
                    and target.guild.id != interaction.guild.id
                ):
                    await self._deny(
                        interaction,
                        GuildConfigError(f"The {kind} must be in this server."),
                    )
                    return
                entity_id: int = target.id
                label = getattr(target, "mention", f"<@{target.id}>")
            else:
                entity_id = _parse_id(target, f"{kind} ID")
                label = self._entity_label(interaction.guild, kind, entity_id)
            if add:
                updated = self.config_service.add_exempt(
                    interaction.guild.id,
                    actor_user_id=interaction.user.id,
                    kind=kind,
                    entity_id=entity_id,
                )
            else:
                updated = self.config_service.remove_exempt(
                    interaction.guild.id,
                    actor_user_id=interaction.user.id,
                    kind=kind,
                    entity_id=entity_id,
                )
        except GuildConfigError as exc:
            await self._deny(interaction, exc)
            return
        field = {
            "user": "exempt_user_ids",
            "role": "exempt_role_ids",
            "channel": "exempt_channel_ids",
        }[kind]
        count = len(getattr(updated, field))
        verb = "Added" if add else "Removed"
        await interaction.response.send_message(
            f"{verb} exempt {kind} {label} ({count} configured).", ephemeral=True
        )
        await self._announce_change(interaction, f"exempt {kind} {verb.lower()}", [field])

    @staticmethod
    def _entity_label(guild: discord.Guild, kind: str, entity_id: int) -> str:
        if kind == "user":
            member = guild.get_member(entity_id)
            return member.mention if member is not None else f"<@{entity_id}>"
        if kind == "role":
            role = guild.get_role(entity_id)
            return role.mention if role is not None else f"<@&{entity_id}>"
        channel = guild.get_channel(entity_id)
        return channel.mention if channel is not None else f"<#{entity_id}>"

    # ----------------------------------------------------------------- reset

    @app_commands.command(name="reset", description="Reset all server configuration to defaults.")
    @app_commands.guild_only()
    @app_commands.describe(
        confirm="Set to true to confirm. Run once without it to see what reset does."
    )
    async def reset(self, interaction: discord.Interaction, confirm: bool = False) -> None:
        """Restore the documented defaults. Moderation cases are never touched."""
        try:
            self._require_admin(interaction)
        except ModerationError as exc:
            await self._deny(interaction, exc)
            return
        if not confirm:
            await interaction.response.send_message(
                "This will reset **all** server configuration (automated moderation, "
                "moderator/administrator roles, exemptions, log channel, purge limit) to "
                "defaults. **Moderation cases are NOT affected.** "
                "Run `/config reset confirm:true` to proceed.",
                ephemeral=True,
            )
            return
        self.config_service.reset(interaction.guild.id, actor_user_id=interaction.user.id)
        await interaction.response.send_message(
            "Server configuration has been reset to defaults. Moderation cases were not touched.",
            ephemeral=True,
        )
        await self._announce_change(interaction, "configuration reset", [])
