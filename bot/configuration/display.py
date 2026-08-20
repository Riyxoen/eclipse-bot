"""Formatting for the ``/config view`` command (Phase 5).

Pure functions: they render a :class:`bot.configuration.guild.GuildConfig`
snapshot into safe, paginated text. Role/user/channel IDs are resolved to
readable Discord mentions where possible; deleted entities render as
``deleted role (id)`` etc. — the bot never crashes over a stale ID.

Nothing sensitive is ever rendered: no tokens, no environment variables, no
internal paths, no database details.
"""

from __future__ import annotations

from typing import Any

from bot.configuration.guild import GuildConfig

#: Lines shown per page in /config view.
PAGE_SIZE = 15


def _role_label(guild: Any, role_id: int) -> str:
    resolver = getattr(guild, "get_role", None)
    role = resolver(role_id) if callable(resolver) else None
    if role is not None:
        return getattr(role, "mention", f"<@&{role_id}>")
    return f"deleted role ({role_id})"


def _user_label(guild: Any, user_id: int) -> str:
    member = guild.get_member(user_id) if getattr(guild, "get_member", None) else None
    if member is not None:
        return getattr(member, "mention", f"<@{user_id}>")
    return f"<@{user_id}>"


def _channel_label(guild: Any, channel_id: int) -> str:
    channel = guild.get_channel(channel_id) if getattr(guild, "get_channel", None) else None
    if channel is not None:
        return getattr(channel, "mention", f"<#{channel_id}>")
    return f"deleted channel ({channel_id})"


def _id_list(guild: Any, ids: tuple[int, ...], label: Any) -> str:
    if not ids:
        return "none"
    return ", ".join(label(guild, entity_id) for entity_id in ids)


def _mention_dimensions(config: GuildConfig) -> str:
    parts = [
        ("user", config.mention_user_threshold),
        ("role", config.mention_role_threshold),
        ("total", config.mention_total_threshold),
        ("@everyone", config.mention_everyone_threshold),
    ]
    rendered = ", ".join(f"{name} ≥{value if value > 0 else 'off'}" for name, value in parts)
    return f"{rendered} → {config.mention_action}"


def _build_lines(config: GuildConfig, guild: Any) -> list[tuple[str, str]]:
    """Return ``(section, line)`` pairs for every setting group."""
    lines: list[tuple[str, str]] = []

    def add(section: str, line: str) -> None:
        lines.append((section, line))

    add("General", f"Automated moderation: {'enabled' if config.automod_enabled else 'disabled'}")
    add("General", f"Moderation logs: {'enabled' if config.mod_log_enabled else 'disabled'}")
    add(
        "General",
        "Log channel: "
        + (_channel_label(guild, config.log_channel_id) if config.log_channel_id else "not set"),
    )
    add("General", f"DM notifications: {'enabled' if config.notify_users else 'disabled'}")
    add("General", f"Maximum purge: {config.max_purge_amount} messages")
    add("General", f"Command prefix: `{config.command_prefix}`")

    add("Roles", "Moderator roles: " + _id_list(guild, config.moderator_role_ids, _role_label))
    add(
        "Roles",
        "Administrator roles: " + _id_list(guild, config.administrator_role_ids, _role_label),
    )

    add(
        "Spam",
        f"{config.spam_threshold} msgs / {config.spam_window_seconds}s → {config.spam_action}",
    )
    add(
        "Spam",
        f"Duplicates: {config.duplicate_threshold} identical / {config.duplicate_window_seconds}s"
        f" → {config.duplicate_action}",
    )
    add("Spam", f"Mentions: {_mention_dimensions(config)}")

    domains = ", ".join(config.allowed_domains) if config.allowed_domains else "none"
    add("Links", f"Link action: {config.link_action} (allowed domains: {domains})")

    terms = ", ".join(config.blocked_terms) if config.blocked_terms else "none"
    mode = "substring" if config.blocked_terms_substring else "whole word"
    add("Word filter", f"Blocked terms ({mode}): {terms} → {config.word_filter_action}")

    invite_codes = ", ".join(config.invite_allowed_codes) if config.invite_allowed_codes else "none"
    add("Invites", f"Invite action: {config.invite_action} (allowed codes: {invite_codes})")
    add(
        "Invites",
        f"Raid protection: ≥{config.raid_join_threshold} joins / {config.raid_window_seconds}s"
        f" → {config.raid_action}",
    )

    add(
        "Exemptions",
        "Users: " + _id_list(guild, config.exempt_user_ids, _user_label),
    )
    add("Exemptions", "Roles: " + _id_list(guild, config.exempt_role_ids, _role_label))
    add("Exemptions", "Channels: " + _id_list(guild, config.exempt_channel_ids, _channel_label))

    if config.escalation:
        spec = ", ".join(f"{count} warnings → {duration}s" for count, duration in config.escalation)
    else:
        spec = "off"
    add("Escalation", f"Warning escalation: {spec}")
    window = config.warning_window_seconds or "never"
    add("Escalation", f"Warning expiry: {window}s")
    add("Escalation", f"Enforcement cooldown: {config.enforcement_cooldown_seconds}s")
    add("Escalation", f"Default timeout: {config.timeout_duration_seconds}s")

    return lines


def format_config_view(config: GuildConfig, guild: Any, *, page: int = 1) -> str:
    """Render the guild's configuration as paginated, safe text."""
    pairs = _build_lines(config, guild)
    total_pages = max(1, (len(pairs) + PAGE_SIZE - 1) // PAGE_SIZE)
    page_number = min(max(int(page), 1), total_pages)

    start = (page_number - 1) * PAGE_SIZE
    chunk = pairs[start : start + PAGE_SIZE]

    rendered: list[str] = []
    if total_pages > 1:
        rendered.append(f"**Server configuration (page {page_number}/{total_pages})**")
    else:
        rendered.append("**Server configuration**")
    current_section: str | None = None
    for section, line in chunk:
        if section != current_section:
            rendered.append(f"__{section}__")
            current_section = section
        rendered.append(f"- {line}")
    if total_pages > 1:
        rendered.append(
            f"Use `/config view page:{page_number + 1}` for more."
            if page_number < total_pages
            else ""
        )
    return "\n".join(line for line in rendered if line)
