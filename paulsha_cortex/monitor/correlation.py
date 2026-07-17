"""Confirmed/inferred source correlation and durable repo overrides."""
from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping, Sequence

import yaml

from .work_models import WorkSource


_WORK_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_LINK_KINDS = frozenset({"github_issue", "github_pr", "openspec", "path"})
_TOP_KEYS = frozenset({"version", "work_items"})
_ITEM_KEYS = frozenset({"title", "links", "excludes"})
_LINK_KEYS = frozenset({"kind", "ref"})


class CorrelationError(ValueError):
    pass


@dataclass(frozen=True, order=True)
class SourceLink:
    kind: str
    ref: str


@dataclass(frozen=True)
class OverrideWorkItem:
    title: str
    links: tuple[SourceLink, ...]
    excludes: tuple[SourceLink, ...]


@dataclass(frozen=True)
class WorkItemOverrides:
    version: int
    work_items: Mapping[str, OverrideWorkItem]


@dataclass(frozen=True)
class InferredSignal:
    work_id: str
    kind: str
    value: str
    source_ids: tuple[str, ...]
    weight: float

    def __post_init__(self) -> None:
        if not _WORK_ID.fullmatch(self.work_id):
            raise CorrelationError(f"invalid inferred work ID: {self.work_id!r}")
        if not self.kind or not self.value or not self.source_ids:
            raise CorrelationError("inferred signal fields must be non-empty")


@dataclass(frozen=True)
class CorrelatedWork:
    work_id: str
    title: str
    sources: tuple[WorkSource, ...]
    confidence: str


@dataclass(frozen=True)
class CorrelationResult:
    groups: tuple[CorrelatedWork, ...]
    source_owners: Mapping[str, str]
    exclusions: tuple[Mapping[str, str], ...]
    explanations: Mapping[str, Mapping]
    degraded: bool = False
    diagnostics: tuple[str, ...] = ()


def override_path(repo_root: str | Path) -> Path:
    return Path(repo_root) / ".cortex" / "work-items.yaml"


def load_work_item_overrides(repo_root: str | Path) -> WorkItemOverrides:
    root = Path(repo_root)
    path = override_path(root)
    if not path.exists():
        return WorkItemOverrides(version=1, work_items={})
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise CorrelationError(f"override parse failed: {error}") from error
    if not isinstance(payload, dict):
        raise CorrelationError("override must be a mapping")
    unknown = set(payload) - _TOP_KEYS
    if unknown:
        raise CorrelationError(f"override contains unknown keys: {sorted(unknown)}")
    if payload.get("version") != 1:
        raise CorrelationError("override version must be exactly 1")
    raw_items = payload.get("work_items")
    if not isinstance(raw_items, dict):
        raise CorrelationError("override work_items must be a mapping")
    items: dict[str, OverrideWorkItem] = {}
    owners: dict[SourceLink, str] = {}
    for work_id, raw_item in raw_items.items():
        if not isinstance(work_id, str) or not _WORK_ID.fullmatch(work_id):
            raise CorrelationError(f"invalid work ID: {work_id!r}")
        if not isinstance(raw_item, dict):
            raise CorrelationError(f"work item {work_id} must be a mapping")
        unknown = set(raw_item) - _ITEM_KEYS
        if unknown:
            raise CorrelationError(
                f"work item {work_id} contains unknown keys: {sorted(unknown)}"
            )
        title = raw_item.get("title")
        if not isinstance(title, str) or not title.strip():
            raise CorrelationError(f"work item {work_id} title must be non-empty")
        links = _parse_links(root, raw_item.get("links", []), field=f"{work_id}.links")
        excludes = _parse_links(
            root, raw_item.get("excludes", []), field=f"{work_id}.excludes"
        )
        if set(links) & set(excludes):
            raise CorrelationError(
                f"source cannot be linked and excluded in work item {work_id}"
            )
        for link in links:
            previous = owners.setdefault(link, work_id)
            if previous != work_id:
                raise CorrelationError(
                    f"confirmed source collision: {link.kind}:{link.ref}"
                )
        items[work_id] = OverrideWorkItem(
            title=title.strip(), links=links, excludes=excludes
        )
    return WorkItemOverrides(version=1, work_items=items)


def _parse_links(root: Path, value: object, *, field: str) -> tuple[SourceLink, ...]:
    if not isinstance(value, list):
        raise CorrelationError(f"{field} must be a list")
    links: list[SourceLink] = []
    for index, row in enumerate(value):
        if not isinstance(row, dict) or set(row) != _LINK_KEYS:
            raise CorrelationError(f"{field}[{index}] must contain only kind/ref")
        kind = row.get("kind")
        ref = row.get("ref")
        if kind not in _LINK_KINDS or not isinstance(ref, str) or not ref:
            raise CorrelationError(f"{field}[{index}] has invalid kind/ref")
        if kind == "path":
            _validate_repo_path(root, ref)
        links.append(SourceLink(kind, ref))
    if len(links) != len(set(links)):
        raise CorrelationError(f"{field} contains duplicate source")
    return tuple(links)


def _validate_repo_path(root: Path, ref: str) -> None:
    candidate = Path(ref)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise CorrelationError(f"path escape is forbidden: {ref}")
    resolved_root = root.resolve()
    resolved = (root / candidate).resolve(strict=False)
    try:
        resolved.relative_to(resolved_root)
    except ValueError as error:
        raise CorrelationError(f"path symlink escape is forbidden: {ref}") from error


def read_frontmatter_work_item(path: str | Path) -> str | None:
    target = Path(path)
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise CorrelationError(f"frontmatter read failed: {target}: {error}") from error
    if not lines or lines[0].strip() != "---":
        return None
    try:
        end = next(index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    except StopIteration as error:
        raise CorrelationError(f"unterminated frontmatter: {target}") from error
    try:
        payload = yaml.safe_load("\n".join(lines[1:end])) or {}
    except yaml.YAMLError as error:
        raise CorrelationError(f"frontmatter parse failed: {target}: {error}") from error
    if not isinstance(payload, dict):
        raise CorrelationError(f"frontmatter must be a mapping: {target}")
    value = payload.get("work_item")
    if value is None:
        return None
    if not isinstance(value, str):
        raise CorrelationError("work_item frontmatter must be a scalar slug")
    if not _WORK_ID.fullmatch(value):
        raise CorrelationError(f"invalid work_item frontmatter slug: {value!r}")
    return value


def correlate_work_sources(
    repo_root: str | Path,
    repo: str,
    sources: Sequence[WorkSource],
    *,
    inferred_signals: Sequence[InferredSignal] = (),
    closing_links: Mapping[str, str] | None = None,
    workflow_links: Mapping[str, str] | None = None,
) -> CorrelationResult:
    root = Path(repo_root)
    try:
        overrides = load_work_item_overrides(root)
    except CorrelationError as error:
        return CorrelationResult(
            groups=(),
            source_owners={},
            exclusions=(),
            explanations={},
            degraded=True,
            diagnostics=(str(error),),
        )
    candidates: dict[str, set[str]] = {source.source_id: set() for source in sources}
    authoritative: dict[str, list[dict[str, str]]] = {}
    exclusions_by_work: dict[str, set[SourceLink]] = {}
    titles = {work_id: item.title for work_id, item in overrides.work_items.items()}
    all_exclusions: list[dict[str, str]] = []
    for work_id, item in overrides.work_items.items():
        exclusions_by_work[work_id] = set(item.excludes)
        all_exclusions.extend(
            {"work_id": work_id, "kind": link.kind, "ref": link.ref}
            for link in item.excludes
        )
        for link in item.links:
            for source in sources:
                if _link_matches_source(link, source):
                    candidates[source.source_id].add(work_id)
                    authoritative.setdefault(work_id, []).append(
                        {"authority": "override", "source_id": source.source_id}
                    )
    for source in sources:
        explicit = _source_frontmatter_work_ids(root, source)
        for work_id in explicit:
            candidates[source.source_id].add(work_id)
            authoritative.setdefault(work_id, []).append(
                {"authority": "frontmatter", "source_id": source.source_id}
            )
    for authority, mapping in (
        ("workflow_metadata", workflow_links or {}),
        ("github_closing", closing_links or {}),
    ):
        for source_id, work_id in mapping.items():
            if source_id not in candidates or not isinstance(work_id, str):
                continue
            if authority == "github_closing" and work_id in candidates:
                linked = candidates[work_id]
                if not linked:
                    target = next(
                        source for source in sources if source.source_id == work_id
                    )
                    linked.add(_fallback_work_id(target))
                for linked_work_id in linked:
                    candidates[source_id].add(linked_work_id)
                    authoritative.setdefault(linked_work_id, []).append(
                        {"authority": authority, "source_id": source_id}
                    )
            elif _WORK_ID.fullmatch(work_id):
                candidates[source_id].add(work_id)
                authoritative.setdefault(work_id, []).append(
                    {"authority": authority, "source_id": source_id}
                )

    diagnostics: list[str] = []
    owners: dict[str, str] = {}
    for source_id, work_ids in candidates.items():
        if len(work_ids) > 1:
            diagnostics.append(
                f"confirmed source collision: {source_id} -> {sorted(work_ids)}"
            )
        elif len(work_ids) == 1:
            owners[source_id] = next(iter(work_ids))
    if diagnostics:
        return CorrelationResult(
            groups=(),
            source_owners={},
            exclusions=tuple(all_exclusions),
            explanations={},
            degraded=True,
            diagnostics=tuple(diagnostics),
        )

    groups: dict[str, list[WorkSource]] = {}
    for source in sources:
        owner = owners.get(source.source_id)
        if owner is not None:
            groups.setdefault(owner, []).append(source)

    signal_groups: dict[str, list[InferredSignal]] = {}
    for signal in inferred_signals:
        signal_groups.setdefault(signal.work_id, []).append(signal)
    eligible = {
        work_id
        for work_id, signals in signal_groups.items()
        if len({signal.kind for signal in signals}) >= 2
    }
    candidate_sources = {
        work_id: set().union(*(set(signal.source_ids) for signal in signals))
        for work_id, signals in signal_groups.items()
    }
    explanations: dict[str, dict] = {}
    for work_id in set((*groups.keys(), *signal_groups.keys(), *overrides.work_items.keys())):
        competitors = sorted(
            other
            for other in eligible
            if other != work_id and candidate_sources.get(work_id, set()) & candidate_sources.get(other, set())
        )
        accepted_inferred = work_id in eligible and not competitors and work_id not in groups
        signal_rows = []
        for signal in signal_groups.get(work_id, []):
            signal_rows.append(
                {
                    "kind": signal.kind,
                    "value": signal.value,
                    "source_ids": list(signal.source_ids),
                    "weight": signal.weight,
                    "accepted": accepted_inferred,
                    "reason": "two independent signals" if accepted_inferred else (
                        "competing candidate" if competitors else "insufficient independent signals"
                    ),
                }
            )
        explanations[work_id] = {
            "work_id": work_id,
            "authoritative_links": authoritative.get(work_id, []),
            "inferred_signals": signal_rows,
            "competing_candidates": competitors,
            "exclusions": [
                {"kind": link.kind, "ref": link.ref}
                for link in sorted(exclusions_by_work.get(work_id, set()))
            ],
            "reducer_trace": [],
        }
        if accepted_inferred:
            selected = []
            excluded = exclusions_by_work.get(work_id, set())
            for source in sources:
                if source.source_id not in candidate_sources[work_id] or source.source_id in owners:
                    continue
                if any(_link_matches_source(link, source) for link in excluded):
                    continue
                selected.append(replace(source, confidence="inferred"))
            if selected:
                groups[work_id] = selected

    correlated: list[CorrelatedWork] = []
    grouped_source_ids: set[str] = set()
    for work_id, grouped in groups.items():
        grouped_source_ids.update(source.source_id for source in grouped)
        correlated.append(
            CorrelatedWork(
                work_id=work_id,
                title=titles.get(work_id, work_id.replace("-", " ")),
                sources=tuple(sorted(grouped, key=lambda source: source.source_id)),
                confidence="confirmed" if any(source.source_id in owners for source in grouped) else "inferred",
            )
        )
    for source in sources:
        if source.source_id in grouped_source_ids:
            continue
        work_id = _fallback_work_id(source)
        correlated.append(
            CorrelatedWork(
                work_id=work_id,
                title=source.ref,
                sources=(source,),
                confidence="inferred",
            )
        )
        explanations.setdefault(
            work_id,
            {
                "work_id": work_id,
                "authoritative_links": [],
                "inferred_signals": [],
                "competing_candidates": [],
                "exclusions": [],
                "reducer_trace": [],
            },
        )
    return CorrelationResult(
        groups=tuple(sorted(correlated, key=lambda group: group.work_id)),
        source_owners=owners,
        exclusions=tuple(all_exclusions),
        explanations=explanations,
    )


def _source_frontmatter_work_ids(root: Path, source: WorkSource) -> tuple[str, ...]:
    if source.kind in {"todo", "superpowers_spec", "superpowers_plan"}:
        value = read_frontmatter_work_item(root / source.ref)
        return (value,) if value else ()
    if source.kind != "openspec":
        return ()
    change = root / "openspec" / "changes" / source.ref
    values: set[str] = set()
    paths = [change / name for name in ("proposal.md", "design.md", "tasks.md")]
    specs = change / "specs"
    if specs.is_dir():
        paths.extend(specs.rglob("*.md"))
    for path in paths:
        if path.is_file() and (value := read_frontmatter_work_item(path)):
            values.add(value)
    return tuple(sorted(values))


def _link_matches_source(link: SourceLink, source: WorkSource) -> bool:
    if link.kind == "path":
        return source.ref == link.ref and source.kind in {
            "todo", "superpowers_spec", "superpowers_plan"
        }
    return source.kind == link.kind and source.ref == link.ref


def _fallback_work_id(source: WorkSource) -> str:
    if source.kind == "github_issue":
        return f"issue:{source.ref}"
    safe = re.sub(r"[^a-z0-9-]+", "-", source.source_id.lower()).strip("-")
    return f"source:{safe}"


def unlink_work_source(repo_root: str | Path, work_id: str, source: SourceLink) -> None:
    root = Path(repo_root)
    overrides = load_work_item_overrides(root)
    if work_id not in overrides.work_items:
        raise CorrelationError(f"unknown work item: {work_id}")
    current = overrides.work_items[work_id]
    updated = OverrideWorkItem(
        title=current.title,
        links=tuple(link for link in current.links if link != source),
        excludes=tuple(sorted(set((*current.excludes, source)))),
    )
    items = dict(overrides.work_items)
    items[work_id] = updated
    _atomic_write_overrides(root, WorkItemOverrides(version=1, work_items=items))


def _atomic_write_overrides(root: Path, overrides: WorkItemOverrides) -> None:
    path = override_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "work_items": {
            work_id: {
                "title": item.title,
                "links": [{"kind": link.kind, "ref": link.ref} for link in item.links],
                "excludes": [{"kind": link.kind, "ref": link.ref} for link in item.excludes],
            }
            for work_id, item in sorted(overrides.work_items.items())
        },
    }
    body = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False).encode("utf-8")
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        os.chmod(path, 0o600)
        dir_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        temp.unlink(missing_ok=True)
        raise
