"""One-time, reversible parking of legacy automatic slice specs."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import date
from pathlib import Path
from typing import Callable

from ..control.contract import atomic_write_json

_SCHEMA = "cortex/legacy-auto-spec-parking/v1"


def park_legacy_auto_specs(
    specs_root: str | Path,
    marker_path: str | Path,
    *,
    parked_date: date | None = None,
    parse_spec: Callable[[Path], dict] | None = None,
) -> tuple[str, ...]:
    """Move existing top-level ``dispatch: auto`` specs into a dated hidden folder.

    The durable marker makes this an adoption/upgrade migration: later auto specs
    remain eligible for dispatch. A parked file can be restored by moving it back.
    """
    root = Path(specs_root).resolve()
    marker = Path(marker_path)
    if marker.exists():
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("legacy spec parking marker is unreadable") from exc
        if payload.get("schema") != _SCHEMA or payload.get("specs_root") != str(root):
            raise RuntimeError("legacy spec parking marker does not match this specs root")
        return ()

    if parse_spec is None:
        from .autonomy import parse_spec_frontmatter

        parse_spec = parse_spec_frontmatter
    root.mkdir(parents=True, exist_ok=True)
    destination_root = root / f".parked-{(parked_date or date.today()).isoformat()}"
    moved: list[dict[str, str]] = []
    for source in sorted(root.glob("*.md")):
        source.lstat()
        if not source.is_file() or source.is_symlink():
            raise RuntimeError(f"legacy slice spec is not a regular file: {source.name}")
        metadata = parse_spec(source)
        if metadata.get("dispatch") != "auto":
            continue
        content_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        destination_root.mkdir(mode=0o700, exist_ok=True)
        destination = destination_root / source.name
        if destination.exists() or destination.is_symlink():
            destination = destination_root / f"{source.stem}-{content_hash[:12]}{source.suffix}"
        if destination.exists() or destination.is_symlink():
            raise RuntimeError(f"legacy slice spec parking destination exists: {destination.name}")
        os.rename(source, destination)
        moved.append(
            {
                "source": source.name,
                "parked": destination.relative_to(root).as_posix(),
                "sha256": content_hash,
            }
        )

    atomic_write_json(
        marker,
        {
            "schema": _SCHEMA,
            "specs_root": str(root),
            "parked": moved,
        },
    )
    return tuple(row["parked"] for row in moved)
