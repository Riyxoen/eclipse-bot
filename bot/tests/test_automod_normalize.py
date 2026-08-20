"""Tests for text/domain normalization and allowlist matching.

Covers the Phase 4 security cases: Unicode normalization, punctuation
separators, and malicious lookalike domains that must never match an
allowlisted domain.
"""

from __future__ import annotations

from bot.automod.normalize import (
    extract_hosts,
    extract_urls,
    is_domain_allowed,
    normalize_domain,
    normalize_text,
)

# ------------------------------------------------------------- text


def test_normalize_lowercases() -> None:
    assert normalize_text("HELLO World") == "hello world"


def test_normalize_strips_punctuation_separators() -> None:
    # Intra-word punctuation collapses: f.u.c.k -> fuck.
    assert normalize_text("F.U.C.K!!!") == "fuck"
    # Word boundaries survive punctuation and spacing.
    assert normalize_text("kill yourself!!!") == "kill yourself"
    assert normalize_text("what the heck?") == "what the heck"
    assert normalize_text("kill, yourself!") == "kill yourself"


def test_normalize_folds_unicode_nfkc() -> None:
    # Full-width characters normalize to ASCII under NFKC.
    assert normalize_text("ｆｕｃｋ") == "fuck"
    assert normalize_text("ＡＳＳ") == "ass"


def test_normalize_collapses_whitespace_and_strips_symbols() -> None:
    assert normalize_text("  Hi   there  ") == "hi there"
    assert normalize_text("😀 emoji ❤️ test") == "emoji test"


def test_normalize_empty_and_non_text() -> None:
    assert normalize_text("") == ""
    assert normalize_text("!!!") == ""
    assert normalize_text("😀😀😀") == ""


# -------------------------------------------------------------- domains


def test_normalize_domain_lowercases_and_strips() -> None:
    assert normalize_domain("WWW.GitHub.COM.") == "github.com"
    assert normalize_domain("github.com") == "github.com"
    assert normalize_domain("  Example.com ") == "example.com"


def test_domain_allowlist_exact_match() -> None:
    assert is_domain_allowed("github.com", ("github.com",)) is True


def test_domain_allowlist_proper_subdomain() -> None:
    assert is_domain_allowed("api.github.com", ("github.com",)) is True
    assert is_domain_allowed("www.github.com", ("github.com",)) is True


def test_domain_allowlist_rejects_lookalikes() -> None:
    # The Phase 4 security cases: these must NEVER match github.com.
    assert is_domain_allowed("evilgithub.com", ("github.com",)) is False
    assert is_domain_allowed("notgithub.com", ("github.com",)) is False
    assert is_domain_allowed("github.com.evil.com", ("github.com",)) is False
    assert is_domain_allowed("github.com.evil", ("github.com",)) is False


def test_domain_allowlist_rejects_unrelated_and_empty() -> None:
    assert is_domain_allowed("example.org", ("github.com",)) is False
    assert is_domain_allowed("", ("github.com",)) is False
    assert is_domain_allowed("github.com", ()) is False


def test_domain_allowlist_multiple_domains() -> None:
    allowed = ("youtube.com", "github.com", "discord.com")
    assert is_domain_allowed("www.youtube.com", allowed) is True
    assert is_domain_allowed("cdn.discord.com", allowed) is True
    assert is_domain_allowed("evil-youtube.com", allowed) is False
    # cdn.discordapp.com is a subdomain of discordapp.com, NOT discord.com.
    assert is_domain_allowed("cdn.discordapp.com", allowed) is False


# ----------------------------------------------------------------- URLs


def test_extract_urls_scheme_based() -> None:
    urls = extract_urls("see https://example.com/a and http://b.org now")
    assert urls == ["https://example.com/a", "http://b.org"]


def test_extract_urls_ignores_bare_words() -> None:
    assert extract_urls("just text example.com") == []


def test_extract_hosts_normalized() -> None:
    hosts = extract_hosts("visit https://www.Example.com/path now")
    assert hosts == ["example.com"]


def test_extract_hosts_empty_without_urls() -> None:
    assert extract_hosts("nothing here") == []
