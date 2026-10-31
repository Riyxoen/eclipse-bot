"""The one canonical rule for the text-command prefix.

Both the environment loader (:mod:`bot.configuration.loader`, for
``RIYXOEN_COMMAND_PREFIX``) and the per-guild configuration model
(:func:`bot.configuration.guild.validate_setting`) validate prefixes through
this module, so an operator's environment value and a guild's
``/config prefix`` can never disagree about what is allowed.
"""

from __future__ import annotations

#: Longest accepted prefix, in characters (matches ``/config prefix``).
MAX_PREFIX_LENGTH = 3

#: Characters that collide with Discord syntax: mentions (``@``), channels
#: (``#``), slash commands (``/``), and role/emoji markup (``<``, ``>``,
#: ``:``).
FORBIDDEN_PREFIX_CHARS = ("@", "#", "/", "<", ">", ":")


def validate_command_prefix(value: str) -> str:
    """Return the cleaned prefix, or raise ``ValueError`` with a safe reason.

    The message is a sentence fragment so callers can prefix it with the
    setting name (e.g. ``RIYXOEN_COMMAND_PREFIX can't be longer than 3
    characters``).
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"must be 1-{MAX_PREFIX_LENGTH} non-space characters")
    cleaned = value.strip()
    if len(cleaned) > MAX_PREFIX_LENGTH:
        raise ValueError(f"can't be longer than {MAX_PREFIX_LENGTH} characters")
    if any(char.isspace() for char in cleaned):
        raise ValueError("can't contain spaces")
    if any(char in cleaned for char in FORBIDDEN_PREFIX_CHARS):
        joined = ", ".join(FORBIDDEN_PREFIX_CHARS)
        raise ValueError(f"can't contain {joined} characters")
    return cleaned
