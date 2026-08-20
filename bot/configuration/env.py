"""Environment variable access and ``.env`` file loading."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

#: Name of the environment variable that holds the bot token.
TOKEN_ENV_VAR = "DISCORD_TOKEN"

#: Default location (relative to the project root) of the local ``.env`` file.
DEFAULT_ENV_FILE = Path(".env")


def load_env_file(path: Path | None = None) -> None:
    """Load a ``.env`` file into the process environment.

    Existing environment variables always win over values from the file.
    A missing file is not an error — the environment may be configured directly.
    """
    candidate = path if path is not None else DEFAULT_ENV_FILE
    if candidate.exists():
        load_dotenv(candidate, override=False)


def getenv(name: str) -> str | None:
    """Read an environment variable, returning ``None`` when unset."""
    return os.getenv(name)
