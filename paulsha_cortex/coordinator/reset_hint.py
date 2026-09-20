"""Parse provider reset hints into stable epoch deadlines."""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone, tzinfo

_RULE_VERSION = "v1"
_RETRY_AFTER_RE = re.compile(r"(?im)^\s*retry-after\s*:\s*(?P<value>[^\s]+)\s*$")
_CODEX_HINT_RE = re.compile(
    r"""
    \btry\ again\ at
    \s+
    (?P<month>[A-Za-z]{3,9})
    \s+
    (?P<day>\d{1,2})(?:st|nd|rd|th)
    \s+
    (?P<hour>\d{1,2})
    :
    (?P<minute>\d{2})
    \s*
    (?P<ampm>AM|PM)
    \b
    """,
    re.IGNORECASE | re.VERBOSE,
)
_MONTH_BY_NAME = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}
_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MAX_EPOCH_DELTA = datetime.max.replace(tzinfo=timezone.utc) - _UNIX_EPOCH
_MAX_EPOCH_SECONDS = _MAX_EPOCH_DELTA.days * 86400 + _MAX_EPOCH_DELTA.seconds


def _coerce_epoch(now: object) -> int | None:
    if isinstance(now, bool):
        return None
    if isinstance(now, int):
        return now
    if isinstance(now, float):
        if not math.isfinite(now):
            return None
        return int(now)
    return None


def _timezone_name(*, reference: datetime, tz: tzinfo) -> str:
    key = getattr(tz, "key", None)
    if isinstance(key, str) and key:
        return key
    name = reference.tzname()
    if isinstance(name, str) and name:
        return name
    rendered = str(tz)
    return rendered if rendered else "UTC"


def _parser_metadata(reference: datetime) -> dict[str, object]:
    assert reference.tzinfo is not None
    return {
        "rule_version": _RULE_VERSION,
        "timezone": _timezone_name(reference=reference, tz=reference.tzinfo),
        "base_year": reference.year,
        "base_reference_epoch": int(reference.timestamp()),
    }


def _parse_retry_after(value: str, *, base_epoch: int) -> int | None:
    try:
        seconds = int(value.strip(), 10)
    except (TypeError, ValueError):
        return None
    if seconds < 0 or seconds > _MAX_EPOCH_SECONDS - base_epoch:
        return None
    return base_epoch + seconds


def _month_number(token: str) -> int | None:
    return _MONTH_BY_NAME.get(token.strip().lower())


def _requires_dst_disambiguation(candidate: datetime, *, tz: tzinfo) -> bool:
    try:
        aware_fold_0 = candidate.replace(tzinfo=tz, fold=0)
        aware_fold_1 = candidate.replace(tzinfo=tz, fold=1)
    except Exception:
        return True
    return aware_fold_0.utcoffset() != aware_fold_1.utcoffset()


def _parse_codex_hint(match: re.Match[str], *, reference: datetime) -> int | None:
    tz = reference.tzinfo
    if tz is None:
        return None
    month = _month_number(match.group("month"))
    if month is None:
        return None
    try:
        day = int(match.group("day"))
        hour = int(match.group("hour"))
        minute = int(match.group("minute"))
    except ValueError:
        return None
    if not 1 <= hour <= 12 or not 0 <= minute <= 59:
        return None
    ampm = match.group("ampm").strip().lower()
    hour = hour % 12
    if ampm == "pm":
        hour += 12
    try:
        naive = datetime(reference.year, month, day, hour, minute)
    except ValueError:
        return None
    if _requires_dst_disambiguation(naive, tz=tz):
        return None
    candidate = naive.replace(tzinfo=tz, fold=0)
    if candidate <= reference:
        return None
    return int(candidate.timestamp())


def parse_reset_hint_details(
    text: str | None,
    *,
    now: object,
    tz: tzinfo | None = None,
) -> tuple[int | None, dict[str, object] | None]:
    base_epoch = _coerce_epoch(now)
    if base_epoch is None:
        return None, None
    haystack = text or ""
    try:
        reference = (_UNIX_EPOCH + timedelta(seconds=base_epoch)).astimezone(
            tz or timezone.utc
        )
    except (OverflowError, ValueError):
        return None, None

    retry_after = _RETRY_AFTER_RE.search(haystack)
    if retry_after is not None:
        parsed = _parse_retry_after(retry_after.group("value"), base_epoch=base_epoch)
        if parsed is None:
            return None, None
        return parsed, _parser_metadata(reference)

    if tz is None:
        return None, None
    codex_hint = _CODEX_HINT_RE.search(haystack)
    if codex_hint is None:
        return None, None
    parsed = _parse_codex_hint(codex_hint, reference=reference)
    if parsed is None:
        return None, None
    return parsed, _parser_metadata(reference)


def parse_reset_hint(
    text: str | None,
    *,
    now: object,
    tz: tzinfo | None = None,
) -> int | None:
    parsed, _ = parse_reset_hint_details(text, now=now, tz=tz)
    return parsed
