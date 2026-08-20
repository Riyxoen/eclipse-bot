"""Text and domain normalization for automated moderation.

Predictable, first-iteration normalization (per the Phase 4 spec: handle
case differences, basic Unicode normalization, and common punctuation
separators — do *not* attempt every possible obfuscation technique):

- Unicode NFKC normalization (full-width/homoglyph folding, e.g. ``ａｓｓ`` -> ``ass``).
- Case folding (``casefold``, stronger than ``lower`` for some scripts).
- Punctuation/symbols/emoji are **dropped**, whitespace is **preserved** and
  collapsed. This is the key trade-off: intra-word punctuation collapses
  (``f.u.c.k`` -> ``fuck``) while real word boundaries survive
  (``you are an ass`` stays three words), so whole-word matching works on
  natural text without matching inside unrelated words.

Domain normalization and allowlist matching are designed to avoid substring
vulnerabilities: ``evilgithub.com`` never matches an allowlisted
``github.com`` because only exact hosts or proper subdomains (``*.github.com``)
match.
"""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlsplit

#: Scheme-based URL detection. Discord only auto-links ``http(s)`` URLs, and
#: keeping the first implementation predictable means we match what Discord
#: itself treats as a link.
_URL_PATTERN = re.compile(r"https?://[^\s<>'\"]+", re.IGNORECASE)


def normalize_text(text: str) -> str:
    """Return a normalized form of ``text`` for comparison.

    NFKC + casefold, drop every non-alphanumeric character that is not
    whitespace, then collapse whitespace runs to single spaces. The result is
    empty for messages with no textual content (e.g. attachments/embeds
    only), which detectors treat as "no content".
    """
    folded = unicodedata.normalize("NFKC", text).casefold()
    kept = "".join(char for char in folded if char.isalnum() or char.isspace())
    return " ".join(kept.split())


def normalize_domain(host: str) -> str:
    """Normalize a URL host for allowlist comparison.

    Lowercases, strips a trailing dot (``github.com.``), and strips a single
    leading ``www.``. Nothing else is rewritten — ``m.``/``ww2.`` etc. are
    left as-is so the allowlist is explicit about what it permits.
    """
    host = host.strip().lower()
    if host.endswith("."):
        host = host[:-1]
    if host.startswith("www."):
        host = host[4:]
    return host


def is_domain_allowed(host: str, allowed_domains: tuple[str, ...]) -> bool:
    """Whether ``host`` matches an allowlisted domain.

    Matching is exact or proper-subdomain only: ``host == allowed`` or
    ``host`` ends with ``\".\" + allowed``. This deliberately rejects
    lookalikes: ``evilgithub.com``, ``github.com.evil.com``, and
    ``notgithub.com`` never match ``github.com``.
    """
    normalized = normalize_domain(host)
    for allowed in allowed_domains:
        domain = normalize_domain(allowed)
        if not domain:
            continue
        if normalized == domain or normalized.endswith("." + domain):
            return True
    return False


def extract_urls(content: str) -> list[str]:
    """Return the ``http(s)`` URLs found in ``content`` (raw matches)."""
    return _URL_PATTERN.findall(content)


def extract_hosts(content: str) -> list[str]:
    """Return normalized hosts of all URLs in ``content``."""
    hosts: list[str] = []
    for url in extract_urls(content):
        host = urlsplit(url).hostname
        if host:
            hosts.append(normalize_domain(host))
    return hosts


def term_pattern(term: str) -> re.Pattern[str]:
    """Compile a whole-word boundary pattern for a normalized blocked term.

    ``\\b`` boundaries keep a term like ``ass`` from matching inside
    ``assessment`` while still matching ``ass`` surrounded by punctuation or
    spacing in the normalized text.
    """
    return re.compile(r"\b" + re.escape(term) + r"\b")
