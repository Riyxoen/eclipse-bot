"""Tests for secret redaction in log output."""

from __future__ import annotations

import io
import logging

from bot.logging.configure import configure_logging
from bot.logging.redaction import RedactingFormatter, SecretRedactionFilter, redact

#: A token-shaped string that matches the redaction pattern (deliberately fake).
TOKEN_LIKE = "A" * 24 + "." + "B" * 10 + "." + "C" * 24


def test_redact_scrubs_token_shaped_strings() -> None:
    assert redact(f"token is {TOKEN_LIKE}, keep this") == "token is [REDACTED], keep this"


def test_redact_scrubs_secret_assignments() -> None:
    assert redact("Authorization: Bearer abc123def456") == "Authorization=[REDACTED]"
    assert redact("token=abc123") == "token=[REDACTED]"


def test_redact_scrubs_configured_secrets() -> None:
    secret = "s3cr3t-value"
    assert redact(f"value={secret} here", secrets=(secret,)) == "value=[REDACTED] here"


def test_redact_leaves_plain_text_alone() -> None:
    assert redact("just a normal message") == "just a normal message"


def test_filter_scrubs_message_and_args() -> None:
    record = logging.LogRecord(
        "test", logging.INFO, __file__, 1, "user %s logged in", (TOKEN_LIKE,), None
    )
    filter_ = SecretRedactionFilter()
    assert filter_.filter(record)
    formatted = record.getMessage()
    assert TOKEN_LIKE not in formatted
    assert "[REDACTED]" in formatted


def test_filter_preserves_tuple_args_for_formatting() -> None:
    """Regression: the filter must not turn the args tuple into a list,
    which would make ``%``-formatting treat it as a single value and corrupt
    every formatted log line (e.g. ``v['0.1.0']`` instead of ``v0.1.0``)."""
    record = logging.LogRecord(
        "test",
        logging.INFO,
        __file__,
        1,
        "case=%s action=%s result=%s",
        (42, "kick", "success"),
        None,
    )
    filter_ = SecretRedactionFilter()
    assert filter_.filter(record)
    assert isinstance(record.args, tuple)
    assert record.getMessage() == "case=42 action=kick result=success"


def test_full_pipeline_renders_formatted_message() -> None:
    """The real filter + formatter path must produce correct output."""
    import io

    from bot.logging.redaction import RedactingFormatter

    stream = io.StringIO()
    logger = logging.getLogger("test.redaction.pipeline")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = logging.StreamHandler(stream)
    handler.setFormatter(RedactingFormatter(secrets=(), fmt="%(message)s"))
    handler.addFilter(SecretRedactionFilter())
    logger.addHandler(handler)
    try:
        logger.info("bot starting (riyxoen v%s)", "0.1.0")
        logger.info("case=%s action=%s target=%s", 1, "kick", 3)
    finally:
        logger.removeHandler(handler)

    output = stream.getvalue()
    assert "bot starting (riyxoen v0.1.0)" in output
    assert "case=1 action=kick target=3" in output
    assert "['0.1.0']" not in output


def test_formatter_scrubs_tracebacks() -> None:
    stream = io.StringIO()
    logger = logging.getLogger("test.redaction.formatter")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = logging.StreamHandler(stream)
    handler.setFormatter(RedactingFormatter(secrets=(), fmt="%(message)s"))
    logger.addHandler(handler)
    try:
        raise ValueError(f"secret in traceback: {TOKEN_LIKE}")
    except ValueError:
        logger.exception("boom")

    output = stream.getvalue()
    assert TOKEN_LIKE not in output
    assert "[REDACTED]" in output


def test_configure_logging_writes_redacted_file(tmp_path, fake_token: str) -> None:
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    log_file = tmp_path / "logs" / "bot.log"
    try:
        configure_logging(level=logging.INFO, log_file=log_file, secrets=(fake_token,))
        logging.getLogger("riyxoen.test").info("configured with token %s", fake_token)
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in original_handlers:
            root.addHandler(handler)
        root.setLevel(original_level)

    content = log_file.read_text(encoding="utf-8")
    assert fake_token not in content
    assert "[REDACTED]" in content
