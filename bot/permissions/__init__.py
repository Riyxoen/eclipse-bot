"""Permission and hierarchy checks (``checks.py``).

Centralized, server-side validation shared by every moderation command:
guild context, moderator permissions/roles, bot permissions, target rules,
role hierarchy, and state checks. Nothing here is duplicated in cogs.
"""
