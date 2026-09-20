"""#928：reset hint parser 的 RED regression 測試。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from importlib import import_module
from zoneinfo import ZoneInfo

import pytest


def _parse_reset_hint():
    for module_name in (
        "paulsha_cortex.coordinator.reset_hint",
        "paulsha_cortex.coordinator.outcome_taxonomy",
    ):
        try:
            module = import_module(module_name)
        except ModuleNotFoundError:
            continue
        parse = getattr(module, "parse_reset_hint", None)
        if callable(parse):
            return parse
    pytest.fail(
        "parse_reset_hint helper is not available from "
        "paulsha_cortex.coordinator.reset_hint or outcome_taxonomy"
    )


def test_retry_after_seconds_resolve_from_explicit_now() -> None:
    parse_reset_hint = _parse_reset_hint()
    now = int(datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc).timestamp())

    assert parse_reset_hint("HTTP 429\nRetry-After: 60", now=now) == now + 60


def test_retry_after_zero_returns_now_without_store_margin() -> None:
    parse_reset_hint = _parse_reset_hint()
    now = int(datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc).timestamp())

    assert parse_reset_hint("Retry-After: 0", now=now) == now


def test_codex_month_day_hint_uses_explicit_timezone_and_current_year() -> None:
    parse_reset_hint = _parse_reset_hint()
    now = int(datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc).timestamp())
    pdt = timezone(timedelta(hours=-7), name="PDT")

    expected = int(datetime(2026, 9, 7, 12, 23, tzinfo=pdt).timestamp())

    assert parse_reset_hint("Try again at Sep 7th 12:23 PM", now=now, tz=pdt) == expected


def test_past_codex_month_day_hint_does_not_roll_forward_to_next_year() -> None:
    parse_reset_hint = _parse_reset_hint()
    now = int(datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc).timestamp())
    pdt = timezone(timedelta(hours=-7), name="PDT")

    assert parse_reset_hint("Try again at Sep 7th 12:23 PM", now=now, tz=pdt) is None


@pytest.mark.parametrize("value", ["-1", "nan", "inf"])
def test_invalid_retry_after_values_are_rejected(value: str) -> None:
    parse_reset_hint = _parse_reset_hint()
    now = int(datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc).timestamp())

    assert parse_reset_hint(f"Retry-After: {value}", now=now) is None


def test_ambiguous_dst_hint_is_rejected() -> None:
    parse_reset_hint = _parse_reset_hint()
    now = int(datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc).timestamp())
    new_york = ZoneInfo("America/New_York")

    assert parse_reset_hint("Try again at Nov 1st 1:30 AM", now=now, tz=new_york) is None
