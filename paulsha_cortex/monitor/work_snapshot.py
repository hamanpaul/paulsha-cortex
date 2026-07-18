"""Atomic durable last-good store for unified Monitor work snapshots."""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from paulsha_cortex.config.paths import work_items_snapshot_path

from .work_models import ProviderSnapshot, WorkItem, parse_timestamp


SNAPSHOT_SCHEMA = "work-items-snapshot/v1"


def work_key(repo: str, work_id: str) -> str:
    """Stable external identity for a repo-scoped Work Item."""
    return f"{repo}::{work_id}"


class SnapshotValidationError(ValueError):
    """The candidate cannot safely replace a last-good snapshot."""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class WorkSnapshot:
    sequence: int
    written_at: str
    providers: Mapping[str, ProviderSnapshot]
    work_items: tuple[WorkItem, ...]
    source_owners: Mapping[str, str]
    exclusions: tuple[Mapping[str, str], ...]

    def __post_init__(self) -> None:
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence < 0:
            raise ValueError("snapshot sequence must be a non-negative integer")
        parse_timestamp(self.written_at)
        object.__setattr__(self, "providers", dict(self.providers))
        owners = dict(self.source_owners)
        by_slug: dict[str, list[str]] = {}
        for item in self.work_items:
            by_slug.setdefault(item.work_id, []).append(work_key(item.repo, item.work_id))
        for source_id, owner in tuple(owners.items()):
            if not isinstance(owner, str):
                continue
            matches = by_slug.get(owner, [])
            if len(matches) == 1:
                owners[source_id] = matches[0]
        object.__setattr__(self, "source_owners", owners)
        object.__setattr__(self, "exclusions", tuple(dict(item) for item in self.exclusions))
        self.validate_ownership()

    def validate_ownership(self) -> None:
        if any(not isinstance(key, str) or not key for key in self.providers):
            raise ValueError("provider IDs must be non-empty strings")
        for provider_id, provider in self.providers.items():
            if provider.provider_id != provider_id:
                raise ValueError("provider map key does not match provider snapshot")
        work_ids = {work_key(item.repo, item.work_id) for item in self.work_items}
        if len(work_ids) != len(self.work_items):
            raise ValueError("duplicate work item ID")
        for source_id, owner in self.source_owners.items():
            if not isinstance(source_id, str) or not source_id:
                raise ValueError("ownership source ID must be non-empty")
            if not isinstance(owner, str) or not owner or owner not in work_ids:
                raise ValueError("ownership must name exactly one existing work item")
        observed_owners: dict[str, str] = {}
        for item in self.work_items:
            for source in item.sources:
                owner = work_key(item.repo, item.work_id)
                previous = observed_owners.setdefault(source.source_id, owner)
                if previous != owner:
                    raise ValueError(
                        f"ownership collision for {source.source_id}: {previous}, {owner}"
                    )
                declared = self.source_owners.get(source.source_id)
                if declared is not None and declared != owner:
                    raise ValueError(f"ownership collision for {source.source_id}")
        for exclusion in self.exclusions:
            if not isinstance(exclusion, Mapping):
                raise ValueError("exclusion must be an object")
            if any(not isinstance(key, str) or not isinstance(value, str) for key, value in exclusion.items()):
                raise ValueError("exclusion keys and values must be strings")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SNAPSHOT_SCHEMA,
            "sequence": self.sequence,
            "written_at": self.written_at,
            "providers": {
                provider_id: self.providers[provider_id].to_dict()
                for provider_id in sorted(self.providers)
            },
            "work_items": [
                item.to_dict()
                for item in sorted(self.work_items, key=lambda row: (row.repo, row.work_id))
            ],
            "source_owners": {
                source_id: self.source_owners[source_id]
                for source_id in sorted(self.source_owners)
            },
            "exclusions": [dict(item) for item in self.exclusions],
        }

    @classmethod
    def from_dict(cls, payload: object) -> "WorkSnapshot":
        if not isinstance(payload, Mapping):
            raise ValueError("snapshot must be an object")
        if payload.get("schema") != SNAPSHOT_SCHEMA:
            raise ValueError(
                f"unsupported snapshot schema: {payload.get('schema')!r}"
            )
        sequence = payload.get("sequence")
        written_at = payload.get("written_at")
        providers = payload.get("providers")
        work_items = payload.get("work_items")
        source_owners = payload.get("source_owners")
        exclusions = payload.get("exclusions")
        if isinstance(sequence, bool) or not isinstance(sequence, int):
            raise ValueError("snapshot sequence must be an integer")
        if not isinstance(written_at, str):
            raise ValueError("snapshot written_at must be a string")
        if not isinstance(providers, Mapping):
            raise ValueError("snapshot providers must be an object")
        if not isinstance(work_items, list):
            raise ValueError("snapshot work_items must be an array")
        if not isinstance(source_owners, Mapping):
            raise ValueError("snapshot source_owners must be an object")
        if not isinstance(exclusions, list):
            raise ValueError("snapshot exclusions must be an array")
        return cls(
            sequence=sequence,
            written_at=written_at,
            providers={
                str(provider_id): ProviderSnapshot.from_dict(str(provider_id), row)
                for provider_id, row in providers.items()
            },
            work_items=tuple(WorkItem.from_dict(item) for item in work_items),
            source_owners=dict(source_owners),
            exclusions=tuple(exclusions),
        )

    def provider_is_fresh(
        self,
        provider_id: str,
        *,
        now: datetime | None = None,
        max_age: int = 900,
    ) -> bool:
        if isinstance(max_age, bool) or not isinstance(max_age, int) or max_age <= 0:
            raise ValueError("max_age must be a positive integer")
        provider = self.providers.get(provider_id)
        if provider is None or provider.last_success_at is None:
            return False
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise ValueError("now must include timezone")
        age = (current - parse_timestamp(provider.last_success_at)).total_seconds()
        return 0 <= age <= max_age


class WorkSnapshotStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else work_items_snapshot_path()

    def load(self) -> WorkSnapshot | None:
        if not self.path.exists():
            return None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeError) as error:
            raise SnapshotValidationError(f"snapshot parse failed: {error}") from error
        return self._validated(payload)

    def load_for_bootstrap(self, *, at: str | None = None) -> WorkSnapshot | None:
        snapshot = self.load()
        if snapshot is None:
            return None
        attempted_at = at or _utcnow()
        providers = {
            provider_id: replace(
                provider,
                status="degraded",
                last_attempt_at=attempted_at,
                diagnostics=tuple(
                    dict.fromkeys((*provider.diagnostics, "awaiting live refresh"))
                ),
            )
            for provider_id, provider in snapshot.providers.items()
        }
        items = tuple(
            replace(item, facets=tuple((*item.facets, "degraded")))
            for item in snapshot.work_items
        )
        return replace(snapshot, providers=providers, work_items=items)

    def write(self, snapshot: WorkSnapshot) -> None:
        try:
            snapshot.validate_ownership()
        except ValueError as error:
            raise SnapshotValidationError(f"snapshot ownership invalid: {error}") from error
        self._write_payload(snapshot.to_dict())

    def write_payload(self, payload: object) -> None:
        snapshot = self._validated(payload)
        self._write_payload(snapshot.to_dict())

    def record_provider_result(
        self,
        result: ProviderSnapshot,
        *,
        work_items: tuple[WorkItem, ...],
        source_owners: Mapping[str, str] | None = None,
        exclusions: tuple[Mapping[str, str], ...] | None = None,
    ) -> WorkSnapshot:
        previous = self.load()
        if previous is None:
            previous = WorkSnapshot(
                sequence=0,
                written_at=result.last_attempt_at,
                providers={},
                work_items=(),
                source_owners={},
                exclusions=(),
            )
        providers = dict(previous.providers)
        prior_provider = providers.get(result.provider_id)
        if result.status == "ok":
            providers[result.provider_id] = result
            next_items = tuple(work_items)
            next_owners = dict(source_owners if source_owners is not None else previous.source_owners)
            next_exclusions = tuple(exclusions if exclusions is not None else previous.exclusions)
        else:
            if prior_provider is None:
                providers[result.provider_id] = replace(
                    result,
                    last_success_at=None,
                    revision=None,
                    sources=(),
                )
            else:
                providers[result.provider_id] = replace(
                    result,
                    last_success_at=prior_provider.last_success_at,
                    revision=prior_provider.revision,
                    sources=prior_provider.sources,
                )
            next_items = previous.work_items
            next_owners = dict(previous.source_owners)
            next_exclusions = previous.exclusions
        updated = WorkSnapshot(
            sequence=previous.sequence + 1,
            written_at=result.last_attempt_at,
            providers=providers,
            work_items=next_items,
            source_owners=next_owners,
            exclusions=next_exclusions,
        )
        self.write(updated)
        return updated

    @staticmethod
    def _validated(payload: object) -> WorkSnapshot:
        try:
            return WorkSnapshot.from_dict(payload)
        except (TypeError, ValueError) as error:
            message = str(error)
            category = "ownership" if "ownership" in message else "schema"
            raise SnapshotValidationError(
                f"snapshot {category} validation failed: {message}"
            ) from error

    def _write_payload(self, payload: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = (
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
        temp_path = Path(temp_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.path)
            os.chmod(self.path, 0o600)
            directory_fd = os.open(self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            temp_path.unlink(missing_ok=True)
            raise
