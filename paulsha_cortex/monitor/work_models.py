"""Versioned work read-model primitives shared by Monitor providers and stores."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping


WORK_SOURCE_KINDS = frozenset(
    {
        "github_issue",
        "github_pr",
        "todo",
        "superpowers_spec",
        "superpowers_plan",
        "openspec",
        "workflow_run",
        "completion_record",
    }
)
WORK_STATES = frozenset({"topic", "todo", "ongoing", "done"})
SOURCE_CONFIDENCES = frozenset({"confirmed", "inferred"})
PROVIDER_STATUSES = frozenset({"ok", "degraded"})


def _required_string(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _optional_string(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is not None and (not isinstance(value, str) or not value):
        raise ValueError(f"{key} must be null or a non-empty string")
    return value


def _strings(value: object, key: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{key} must be an array of strings")
    return tuple(value)


def parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"invalid timestamp: {value!r}") from error
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp must include timezone: {value!r}")
    return parsed


@dataclass(frozen=True)
class WorkSource:
    source_id: str
    kind: str
    ref: str
    revision: str
    status: str
    confidence: str
    provider: str

    def __post_init__(self) -> None:
        for field_name in ("source_id", "ref", "revision", "status", "provider"):
            if not isinstance(getattr(self, field_name), str) or not getattr(self, field_name):
                raise ValueError(f"WorkSource.{field_name} must be a non-empty string")
        if self.kind not in WORK_SOURCE_KINDS:
            raise ValueError(f"unsupported WorkSource kind: {self.kind!r}")
        if self.confidence not in SOURCE_CONFIDENCES:
            raise ValueError(f"unsupported source confidence: {self.confidence!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "kind": self.kind,
            "ref": self.ref,
            "revision": self.revision,
            "status": self.status,
            "confidence": self.confidence,
            "provider": self.provider,
        }

    @classmethod
    def from_dict(cls, payload: object) -> "WorkSource":
        if not isinstance(payload, Mapping):
            raise ValueError("WorkSource must be an object")
        return cls(
            source_id=_required_string(payload, "source_id"),
            kind=_required_string(payload, "kind"),
            ref=_required_string(payload, "ref"),
            revision=_required_string(payload, "revision"),
            status=_required_string(payload, "status"),
            confidence=_required_string(payload, "confidence"),
            provider=_required_string(payload, "provider"),
        )


@dataclass(frozen=True)
class ProviderSnapshot:
    provider_id: str
    status: str
    last_attempt_at: str
    last_success_at: str | None
    revision: str | None
    diagnostics: tuple[str, ...]
    sources: tuple[WorkSource, ...]
    observations: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.provider_id:
            raise ValueError("ProviderSnapshot.provider_id must be non-empty")
        if self.status not in PROVIDER_STATUSES:
            raise ValueError(f"unsupported provider status: {self.status!r}")
        parse_timestamp(self.last_attempt_at)
        if self.last_success_at is not None:
            parse_timestamp(self.last_success_at)
        if self.status == "ok" and (self.last_success_at is None or self.revision is None):
            raise ValueError("successful provider snapshot requires success time and revision")
        if self.revision is not None and not self.revision:
            raise ValueError("provider revision must be null or non-empty")
        if any(not isinstance(item, str) or not item for item in self.diagnostics):
            raise ValueError("provider diagnostics must contain non-empty strings")
        source_ids = [source.source_id for source in self.sources]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("provider snapshot contains duplicate source_id")
        if any(source.provider != self.provider_id for source in self.sources):
            raise ValueError("provider snapshot contains foreign provider source")
        observations = self.observations
        if not isinstance(observations, Mapping):
            raise ValueError("provider observations must be an object")
        try:
            normalized_observations = json.loads(json.dumps(observations))
        except (TypeError, ValueError) as error:
            raise ValueError("provider observations must be JSON-safe") from error
        object.__setattr__(self, "observations", normalized_observations)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "last_attempt_at": self.last_attempt_at,
            "last_success_at": self.last_success_at,
            "revision": self.revision,
            "diagnostics": list(self.diagnostics),
            "sources": [source.to_dict() for source in self.sources],
            "observations": dict(self.observations),
        }

    @classmethod
    def from_dict(cls, provider_id: str, payload: object) -> "ProviderSnapshot":
        if not isinstance(payload, Mapping):
            raise ValueError("ProviderSnapshot must be an object")
        sources = payload.get("sources")
        if not isinstance(sources, list):
            raise ValueError("provider sources must be an array")
        return cls(
            provider_id=provider_id,
            status=_required_string(payload, "status"),
            last_attempt_at=_required_string(payload, "last_attempt_at"),
            last_success_at=_optional_string(payload, "last_success_at"),
            revision=_optional_string(payload, "revision"),
            diagnostics=_strings(payload.get("diagnostics"), "diagnostics"),
            sources=tuple(WorkSource.from_dict(source) for source in sources),
            observations=(
                payload.get("observations", {})
                if isinstance(payload.get("observations", {}), Mapping)
                else _invalid_observations()
            ),
        )


def _invalid_observations():
    raise ValueError("provider observations must be an object")


@dataclass(frozen=True)
class WorkItem:
    work_id: str
    repo: str
    title: str
    state: str
    phase: str | None
    facets: tuple[str, ...]
    sources: tuple[WorkSource, ...]
    next_actions: tuple[str, ...]
    workflow_run_id: str | None
    updated_at: str

    def __post_init__(self) -> None:
        for field_name in ("work_id", "repo", "title", "updated_at"):
            if not isinstance(getattr(self, field_name), str) or not getattr(self, field_name):
                raise ValueError(f"WorkItem.{field_name} must be a non-empty string")
        normalized_state = "ongoing" if self.state == "on-going" else self.state
        if normalized_state not in WORK_STATES:
            raise ValueError(f"unsupported work state: {self.state!r}")
        object.__setattr__(self, "state", normalized_state)
        parse_timestamp(self.updated_at)
        for field_name in ("phase", "workflow_run_id"):
            value = getattr(self, field_name)
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError(f"WorkItem.{field_name} must be null or non-empty")
        if any(not isinstance(item, str) or not item for item in self.facets):
            raise ValueError("facets must contain non-empty strings")
        if any(not isinstance(item, str) or not item for item in self.next_actions):
            raise ValueError("next_actions must contain non-empty strings")
        object.__setattr__(self, "facets", tuple(sorted(set(self.facets))))
        object.__setattr__(self, "next_actions", tuple(sorted(set(self.next_actions))))

    def to_dict(self) -> dict[str, Any]:
        return {
            "work_id": self.work_id,
            "repo": self.repo,
            "title": self.title,
            "state": "on-going" if self.state == "ongoing" else self.state,
            "phase": self.phase,
            "facets": list(self.facets),
            "sources": [source.to_dict() for source in self.sources],
            "next_actions": list(self.next_actions),
            "workflow_run_id": self.workflow_run_id,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, payload: object) -> "WorkItem":
        if not isinstance(payload, Mapping):
            raise ValueError("WorkItem must be an object")
        sources = payload.get("sources")
        if not isinstance(sources, list):
            raise ValueError("work item sources must be an array")
        return cls(
            work_id=_required_string(payload, "work_id"),
            repo=_required_string(payload, "repo"),
            title=_required_string(payload, "title"),
            state=_required_string(payload, "state"),
            phase=_optional_string(payload, "phase"),
            facets=_strings(payload.get("facets"), "facets"),
            sources=tuple(WorkSource.from_dict(source) for source in sources),
            next_actions=_strings(payload.get("next_actions"), "next_actions"),
            workflow_run_id=_optional_string(payload, "workflow_run_id"),
            updated_at=_required_string(payload, "updated_at"),
        )
