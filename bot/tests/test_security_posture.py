"""Phase 11: startup/configuration security-posture tests.

These guard the invariants the startup layer depends on:

- The local ``.env`` file (which may hold the real bot token) is never
  tracked by Git.
- ``.env.example`` ships the documented placeholder token only — never a
  real credential — and the placeholder matches what the loader rejects.
- The CLI reports configuration failures safely: it names the missing
  variable but never echoes a token-shaped value.

The CLI tests exercise the real ``bot.cli.main`` entrypoint with the
environment cleaned by the shared ``clean_bot_environment`` fixture, so they
never touch a real token.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

# The sentinel the configuration loader rejects at startup. Importing it
# keeps this test in sync: if the placeholder changes, both the loader and
# this check must agree.
from bot.configuration.loader import _PLACEHOLDER_TOKEN

#: Project root (``bot/tests/`` -> repo root).
REPO_ROOT = Path(__file__).resolve().parents[2]

#: Shape of a Discord bot token: three dot-separated base64-ish segments.
TOKEN_SHAPE = re.compile(r"[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{20,}")

GIT_AVAILABLE = shutil.which("git") is not None


def _git_check_ignore(path: str) -> bool:
    """Return ``True`` when git reports ``path`` as ignored (from repo root)."""
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "check-ignore", "-q", path],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


# ------------------------------------------------------------------ .gitignore


def test_gitignore_ignores_env_file() -> None:
    """``.gitignore`` must cover ``.env`` while keeping the template trackable."""
    rules = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".env" in rules
    assert "!.env.example" in rules


@pytest.mark.skipif(not GIT_AVAILABLE, reason="git is not available")
def test_git_check_ignore_confirms_env_ignored() -> None:
    assert _git_check_ignore(".env") is True


@pytest.mark.skipif(not GIT_AVAILABLE, reason="git is not available")
def test_git_check_ignore_keeps_env_example_tracked() -> None:
    assert _git_check_ignore(".env.example") is False


# --------------------------------------------------------------- .env.example


def test_env_example_uses_only_the_placeholder_token() -> None:
    """``.env.example`` carries the documented placeholder, never a real token."""
    content = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    token_line = next(line for line in content.splitlines() if line.startswith("DISCORD_TOKEN="))
    assert token_line == f"DISCORD_TOKEN={_PLACEHOLDER_TOKEN}"
    # No credential-shaped string may lurk anywhere in the template.
    assert TOKEN_SHAPE.search(content) is None


# ------------------------------------------------------ CLI safe error handling


def test_cli_main_missing_token_prints_safe_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A missing ``DISCORD_TOKEN`` exits 2 and never echoes a token shape."""
    from bot.cli import main

    assert main([]) == 2

    captured = capsys.readouterr()
    assert "DISCORD_TOKEN" in captured.err
    assert "is not configured" in captured.err
    assert TOKEN_SHAPE.search(captured.err) is None


def test_cli_check_fails_when_token_missing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``--check`` reports the missing token as a startup failure (exit 1).

    ``--check`` reports through the logging system (not stderr), so assert on
    the captured log records and verify no token shape leaks.
    """
    from bot.cli import main

    assert main(["--check"]) == 1

    assert "DISCORD_TOKEN" in caplog.text
    assert "is not configured" in caplog.text
    assert TOKEN_SHAPE.search(caplog.text) is None


def test_cli_check_passes_with_fake_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_token: str,
) -> None:
    """A valid (fake) token passes the full startup configuration path."""
    from bot.cli import main

    monkeypatch.setenv("DISCORD_TOKEN", fake_token)
    monkeypatch.setenv("RIYXOEN_DATABASE_PATH", str(tmp_path / "cases.db"))

    assert main(["--check"]) == 0
