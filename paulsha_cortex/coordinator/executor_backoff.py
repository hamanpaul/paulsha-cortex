"""Durable executor backoff store reader.

This GREEN card intentionally implements only the strict read surface:
callers must be able to distinguish a missing store from unreadable or
corrupt persisted state.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

__all__ = ["StoreObservation", "StoreRead", "read_store"]


class StoreObservation(str, Enum):
    MISSING = "missing"
    PRESENT = "present"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class StoreRead:
    observation: StoreObservation
    payload: dict[str, object] | None = None
    diagnostics: tuple[str, ...] = ()


def _unknown(*diagnostics: str) -> StoreRead:
    return StoreRead(observation=StoreObservation.UNKNOWN, diagnostics=tuple(diagnostics))


def read_store(store_path: str | Path) -> StoreRead:
    path = Path(store_path)
    try:
        path.lstat()
    except FileNotFoundError:
        return StoreRead(observation=StoreObservation.MISSING)
    except OSError as error:
        return _unknown(f"unable to stat executor backoff store: {error}")

    try:
        raw = path.read_bytes()
    except OSError as error:
        return _unknown(f"unable to read executor backoff store: {error}")

    try:
        payload = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as error:
        return _unknown(f"executor backoff store is not valid UTF-8: {error}")
    except json.JSONDecodeError as error:
        return _unknown(f"executor backoff store is not valid JSON: {error}")

    if not isinstance(payload, dict):
        return _unknown(
            "executor backoff store payload must be a JSON object, "
            f"got {type(payload).__name__}"
        )
    return StoreRead(observation=StoreObservation.PRESENT, payload=payload)
