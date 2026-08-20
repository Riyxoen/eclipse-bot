"""User-facing formatting of case records.

Response text contains only safe, public information (case ID, action,
target/mod names, reason, status). It never includes secrets, internal
exception details, or message contents. Reasons are truncated in list views
so a Discord message never grows unbounded.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC

from bot.moderation.cases import STATUS_CLEARED, CaseRecord
from bot.moderation.validation import humanize_duration
from bot.services.cases import CasePage

#: Maximum length of a reason inside a list view before it is truncated.
_LIST_REASON_LIMIT = 60


def format_case_response(
    record: CaseRecord,
    *,
    target_label: str,
    moderator_label: str,
) -> str:
    """Render a case record as the confirmation message shown to the user.

    ``target_label`` / ``moderator_label`` are display names (e.g. ``@user``)
    supplied by the command layer, since the record only stores IDs.
    """
    lines = [
        f"**Case #{record.case_id}**",
        f"Action: {record.action.title()}",
        f"Target: {target_label}",
        f"Moderator: {moderator_label}",
        f"Reason: {record.reason or '—'}",
    ]
    if record.duration_seconds is not None:
        lines.append(f"Duration: {humanize_duration(record.duration_seconds)}")
    lines.append(f"Status: {_status_label(record)}")
    return "\n".join(lines)


def format_case_detail(
    record: CaseRecord,
    *,
    target_label: str,
    moderator_label: str,
) -> str:
    """Render the full single-case view for ``/case``."""
    lines = [
        f"**Case #{record.case_id}**",
        f"Action: {record.action.title()}",
        f"Source: {_source_label(record)}",
        f"Target: {target_label}",
        f"Moderator: {moderator_label}",
        f"Reason: {record.reason or '—'}",
        f"Created: {_format_utc(record.created_at)}",
    ]
    if record.duration_seconds is not None:
        lines.append(f"Duration: {humanize_duration(record.duration_seconds)}")
        if record.expires_at is not None:
            lines.append(f"Expires: {_format_utc(record.expires_at)}")
    lines.append(f"Status: {_status_label(record)}")
    return "\n".join(lines)


def format_case_list(
    page: CasePage,
    *,
    member_label: str,
    label: Callable[[int], str],
) -> str:
    """Render a paginated list of cases for ``/cases`` (one line per case)."""
    header = (
        f"**Moderation cases for {member_label}** — "
        f"page {page.page} of {page.total_pages} ({page.total} total)"
    )
    lines = [header]
    for record in page.items:
        action = record.action.title()
        if record.automated:
            action += " 🤖"
        lines.append(
            f"`#{record.case_id}` {action} | {label(record.moderator_user_id)} | "
            f"{_truncate(record.reason, _LIST_REASON_LIMIT)} | "
            f"{record.created_at:%Y-%m-%d} | {_status_label(record)}"
        )
    if page.has_more:
        lines.append(f"_Page {page.page + 1} has more cases — use `page {page.page + 1}`._")
    return "\n".join(lines)


# --------------------------------------------------------------- helpers


def _status_label(record: CaseRecord) -> str:
    if record.status == STATUS_CLEARED:
        return "Cleared"
    return "Success" if record.success else "Failed"


def _source_label(record: CaseRecord) -> str:
    """Human-readable source of a case (automated detector vs manual)."""
    if record.automated:
        return f"Automated ({record.detector or 'detector'})"
    return "Manual"


def _format_utc(dt) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _truncate(text: str, limit: int) -> str:
    text = text or "—"
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"
