"""Service layer.

- ``moderation.py`` — moderation actions (warn/timeout/kick/ban/unban/purge)
- ``cases.py`` — case management (create/get/list/update, guild isolation)
- ``notifications.py`` — best-effort DM notifications
- ``guild_config.py`` — per-guild configuration (load/update/reset, caching,
  audit; consumed by the moderation service and the automod engine)

Cogs stay thin and delegate here; the services never touch Discord responses.
"""
