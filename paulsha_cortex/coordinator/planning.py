from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping

from .._yaml import YAMLError, safe_load
from .model_identities import (
    CapabilityProbe,
    IdentityRegistry,
    ModelIdentity,
    select_secondary_planner,
)
from .workflow import GateEvidenceRef

PLANNING_KINDS = ("spec", "design", "plan")
QUESTION_PACK_SCHEMA_VERSION = 1
BRAINSTORM_EVIDENCE_SCHEMA_VERSION = 1
_STANDALONE_MARKERS = frozenset({"tbd", "[tbd]", "decision: tbd", "決策：未定"})
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_LIST_ITEM_RE = re.compile(r"^(?:[-+*]|\d+[.)])\s+(\S.*)$")
_REQUIRED_HEADINGS = {
    "spec": frozenset({"requirements", "requirement", "problem", "problem and outcome", "goals"}),
    "design": frozenset({"decisions", "decision", "design", "architecture"}),
    "plan": frozenset({"task", "tasks"}),
}


def _canonical_json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash_payload(payload: object) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PlanningScope:
    repo: str
    work_id: str
    source_revision: str

    def __post_init__(self) -> None:
        for field, value in (
            ("repo", self.repo),
            ("work_id", self.work_id),
            ("source_revision", self.source_revision),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"planning scope {field} must be a non-empty string")

    def to_dict(self) -> dict[str, str]:
        return {
            "repo": self.repo.strip(),
            "work_id": self.work_id.strip(),
            "source_revision": self.source_revision.strip(),
        }


@dataclass(frozen=True)
class PlanningArtifact:
    kind: str
    ref: str
    text: str


@dataclass(frozen=True)
class BlockingMarker:
    kind: str
    line: int
    text: str


@dataclass(frozen=True)
class ArtifactAssessment:
    artifact: PlanningArtifact
    accepted: bool
    reasons: tuple[str, ...]
    blocking_markers: tuple[BlockingMarker, ...]


def _frontmatter_and_body(text: str) -> tuple[dict[str, object], str, int]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text, 0
    closing = next((index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"), None)
    if closing is None:
        return {}, text, 0
    frontmatter_lines = lines[1:closing]
    seen_top_level: set[str] = set()
    for raw in frontmatter_lines:
        if not raw or raw[0].isspace() or ":" not in raw:
            continue
        key = raw.split(":", 1)[0].strip()
        if key in seen_top_level:
            return {}, "\n".join(lines[closing + 1 :]), closing + 1
        seen_top_level.add(key)
    try:
        payload = safe_load("\n".join(frontmatter_lines))
    except YAMLError:
        return {}, "\n".join(lines[closing + 1 :]), closing + 1
    if not isinstance(payload, dict):
        payload = {}
    return payload, "\n".join(lines[closing + 1 :]), closing + 1


def _headings_and_markers(body: str, *, line_offset: int) -> tuple[set[str], tuple[BlockingMarker, ...]]:
    headings: set[str] = set()
    markers: list[BlockingMarker] = []
    in_fence = False
    fence_token: str | None = None
    open_questions_level: int | None = None
    for body_index, raw in enumerate(body.splitlines(), start=1):
        stripped = raw.strip()
        if stripped.startswith(("```", "~~~")):
            token = stripped[:3]
            if not in_fence:
                in_fence = True
                fence_token = token
            elif token == fence_token:
                in_fence = False
                fence_token = None
            continue
        if in_fence:
            continue
        heading = _HEADING_RE.match(stripped)
        if heading:
            level = len(heading.group(1))
            title = heading.group(2).strip().casefold()
            title = re.sub(r"^\d+(?:\.\d+)*[.)]?\s+", "", title).rstrip(":：")
            headings.add(title)
            if title in {"open questions", "open question", "未決問題"}:
                open_questions_level = level
            elif open_questions_level is not None and level <= open_questions_level:
                open_questions_level = None
            continue
        line_number = line_offset + body_index
        if stripped.casefold() in _STANDALONE_MARKERS:
            markers.append(BlockingMarker("standalone", line_number, stripped))
            continue
        item = _LIST_ITEM_RE.match(stripped)
        if open_questions_level is not None:
            if item and item.group(1).strip().casefold() not in {"none", "n/a", "無", "無。"}:
                markers.append(BlockingMarker("open-question", line_number, item.group(1).strip()))
    return headings, tuple(markers)


def _has_required_heading(kind: str, headings: set[str]) -> bool:
    if kind == "plan":
        return any(title in {"task", "tasks"} or title.startswith("task ") for title in headings)
    required = _REQUIRED_HEADINGS[kind]
    return any(title in required for title in headings)


def assess_planning_artifact(artifact: PlanningArtifact) -> ArtifactAssessment:
    if artifact.kind not in PLANNING_KINDS:
        raise ValueError(f"unknown planning artifact kind: {artifact.kind}")
    frontmatter, body, offset = _frontmatter_and_body(artifact.text)
    headings, markers = _headings_and_markers(body, line_offset=offset)
    reasons: list[str] = []
    status = frontmatter.get("status")
    if not isinstance(status, str) or status.strip().casefold() != "accepted":
        reasons.append("status-not-accepted")
    if not _has_required_heading(artifact.kind, headings):
        reasons.append("required-section-missing")
    if markers:
        reasons.append("blocking-decision")
    return ArtifactAssessment(artifact, not reasons, tuple(reasons), markers)


@dataclass(frozen=True)
class PlanningQuestion:
    question_id: str
    kind: str
    prompt: str
    source_refs: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "question_id": self.question_id,
            "kind": self.kind,
            "prompt": self.prompt,
            "source_refs": list(self.source_refs),
        }


@dataclass(frozen=True)
class QuestionPack:
    pack_id: str
    questions: tuple[PlanningQuestion, ...]
    schema_version: int = QUESTION_PACK_SCHEMA_VERSION

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "pack_id": self.pack_id,
            "questions": [question.to_dict() for question in self.questions],
        }


@dataclass(frozen=True)
class CompletenessReport:
    complete: bool
    assessments: tuple[ArtifactAssessment, ...]
    missing_kinds: tuple[str, ...]
    default_question_pack: QuestionPack

    def to_dict(self) -> dict[str, object]:
        return {
            "complete": self.complete,
            "missing_kinds": list(self.missing_kinds),
            "artifacts": [
                {
                    "kind": assessment.artifact.kind,
                    "ref": assessment.artifact.ref,
                    "accepted": assessment.accepted,
                    "reasons": list(assessment.reasons),
                    "blocking_markers": [asdict(marker) for marker in assessment.blocking_markers],
                }
                for assessment in self.assessments
            ],
        }


def _make_question(kind: str, prompt: str, source_refs: tuple[str, ...]) -> PlanningQuestion:
    identity = {"kind": kind, "prompt": prompt, "source_refs": list(source_refs)}
    return PlanningQuestion(
        question_id="q-" + _hash_payload(identity)[:16],
        kind=kind,
        prompt=prompt,
        source_refs=source_refs,
    )


def _build_default_question_pack(
    assessments: tuple[ArtifactAssessment, ...], missing_kinds: tuple[str, ...]
) -> QuestionPack:
    questions: list[PlanningQuestion] = []
    for kind in missing_kinds:
        questions.append(
            _make_question(
                f"missing-{kind}",
                f"What authoritative content is required to create an accepted {kind}?",
                (),
            )
        )
    for assessment in assessments:
        if not assessment.blocking_markers:
            continue
        questions.append(
            _make_question(
                "blocking-decision",
                f"What evidence resolves the blocking decision in {assessment.artifact.ref}?",
                (assessment.artifact.ref,),
            )
        )
    body = [question.to_dict() for question in questions]
    return QuestionPack(pack_id="qp-" + _hash_payload(body)[:24], questions=tuple(questions))


def assess_planning_completeness(artifacts: Iterable[PlanningArtifact]) -> CompletenessReport:
    assessments = tuple(assess_planning_artifact(artifact) for artifact in artifacts)
    accepted_kinds = {assessment.artifact.kind for assessment in assessments if assessment.accepted}
    missing_kinds = tuple(kind for kind in PLANNING_KINDS if kind not in accepted_kinds)
    pack = _build_default_question_pack(assessments, missing_kinds)
    has_blockers = any(assessment.blocking_markers for assessment in assessments)
    return CompletenessReport(not missing_kinds and not has_blockers, assessments, missing_kinds, pack)


def _strict_string_list(value: object, field: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"{field} must be a string list")
    normalized = tuple(item.strip() for item in value)
    if not allow_empty and not normalized:
        raise ValueError(f"{field} must not be empty")
    return normalized


def validate_question_pack(payload: object, *, report: CompletenessReport) -> QuestionPack:
    if not isinstance(payload, dict):
        raise ValueError("question pack must be an object")
    extras = set(payload) - {"schema_version", "pack_id", "questions"}
    if extras:
        raise ValueError(f"question pack unexpected key: {sorted(extras)[0]}")
    if payload.get("schema_version") != QUESTION_PACK_SCHEMA_VERSION:
        raise ValueError("question pack schema_version invalid")
    pack_id = payload.get("pack_id")
    rows = payload.get("questions")
    if not isinstance(pack_id, str) or not pack_id or not isinstance(rows, list):
        raise ValueError("question pack identity/questions invalid")
    questions: list[PlanningQuestion] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"questions[{index}] must be an object")
        extras = set(row) - {"question_id", "kind", "prompt", "source_refs"}
        if extras:
            raise ValueError(f"questions[{index}] unexpected key: {sorted(extras)[0]}")
        question_id = row.get("question_id")
        kind = row.get("kind")
        prompt = row.get("prompt")
        if not all(isinstance(value, str) and value.strip() for value in (question_id, kind, prompt)):
            raise ValueError(f"questions[{index}] has invalid scalar")
        if question_id in seen:
            raise ValueError(f"duplicate question_id: {question_id}")
        seen.add(question_id)
        questions.append(
            PlanningQuestion(
                question_id=question_id.strip(),
                kind=kind.strip(),
                prompt=prompt.strip(),
                source_refs=_strict_string_list(row.get("source_refs"), f"questions[{index}].source_refs", allow_empty=True),
            )
        )
    normalized = QuestionPack(pack_id=pack_id, questions=tuple(questions))
    if normalized.to_dict() != report.default_question_pack.to_dict():
        raise ValueError("question pack does not cover exact completeness blockers")
    return normalized


@dataclass(frozen=True)
class SecondaryEvidenceItem:
    question_id: str
    claims: tuple[str, ...]
    source_refs: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "question_id": self.question_id,
            "claims": list(self.claims),
            "source_refs": list(self.source_refs),
        }


@dataclass(frozen=True)
class SecondaryEvidence:
    question_pack_id: str
    items: tuple[SecondaryEvidenceItem, ...]
    schema_version: int = 1

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "question_pack_id": self.question_pack_id,
            "evidence": [item.to_dict() for item in self.items],
        }


def validate_secondary_evidence(payload: object, *, question_pack: QuestionPack) -> SecondaryEvidence:
    if not isinstance(payload, dict):
        raise ValueError("secondary evidence must be an object")
    extras = set(payload) - {"schema_version", "question_pack_id", "evidence"}
    if extras:
        raise ValueError(f"secondary evidence unexpected key: {sorted(extras)[0]}")
    if payload.get("schema_version") != 1 or payload.get("question_pack_id") != question_pack.pack_id:
        raise ValueError("secondary evidence identity invalid")
    rows = payload.get("evidence")
    if not isinstance(rows, list):
        raise ValueError("secondary evidence must be a list")
    expected = {question.question_id for question in question_pack.questions}
    seen: set[str] = set()
    items: list[SecondaryEvidenceItem] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"evidence[{index}] must be an object")
        extras = set(row) - {"question_id", "claims", "source_refs"}
        if extras:
            raise ValueError(f"evidence[{index}] unexpected key: {sorted(extras)[0]}")
        question_id = row.get("question_id")
        if not isinstance(question_id, str) or question_id not in expected or question_id in seen:
            raise ValueError(f"evidence[{index}].question_id invalid")
        seen.add(question_id)
        items.append(
            SecondaryEvidenceItem(
                question_id=question_id,
                claims=_strict_string_list(row.get("claims"), f"evidence[{index}].claims"),
                source_refs=_strict_string_list(row.get("source_refs"), f"evidence[{index}].source_refs"),
            )
        )
    if seen != expected:
        raise ValueError("secondary evidence does not cover every question")
    return SecondaryEvidence(question_pack.pack_id, tuple(items))


def _validate_primary_integration(
    payload: object,
    *,
    question_pack: QuestionPack,
    secondary_evidence_hash: str,
) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise ValueError("primary integration must be an object")
    extras = set(payload) - {
        "schema_version",
        "question_pack_id",
        "secondary_evidence_hash",
        "resolutions",
    }
    if extras:
        raise ValueError(f"primary integration unexpected key: {sorted(extras)[0]}")
    if payload.get("schema_version") != 1:
        raise ValueError("primary integration schema invalid")
    if payload.get("question_pack_id") != question_pack.pack_id:
        raise ValueError("primary integration pack mismatch")
    if payload.get("secondary_evidence_hash") != secondary_evidence_hash:
        raise ValueError("primary integration evidence hash mismatch")
    rows = payload.get("resolutions")
    if not isinstance(rows, list):
        raise ValueError("primary integration resolutions must be a list")
    expected = {question.question_id for question in question_pack.questions}
    seen: set[str] = set()
    normalized: list[dict[str, object]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != {
            "question_id",
            "decision",
            "artifact_kind",
            "artifact_refs",
        }:
            raise ValueError(f"resolutions[{index}] invalid keys")
        question_id = row.get("question_id")
        decision = row.get("decision")
        if not isinstance(question_id, str) or question_id not in expected or question_id in seen:
            raise ValueError(f"resolutions[{index}].question_id invalid")
        if not isinstance(decision, str) or not decision.strip():
            raise ValueError(f"resolutions[{index}].decision invalid")
        artifact_kind = row.get("artifact_kind")
        if artifact_kind not in PLANNING_KINDS:
            raise ValueError(f"resolutions[{index}].artifact_kind invalid")
        question = next(item for item in question_pack.questions if item.question_id == question_id)
        if question.kind.startswith("missing-") and artifact_kind != question.kind.removeprefix("missing-"):
            raise ValueError(f"resolutions[{index}].artifact_kind mismatch")
        seen.add(question_id)
        normalized.append(
            {
                "question_id": question_id,
                "decision": decision.strip(),
                "artifact_kind": artifact_kind,
                "artifact_refs": list(
                    _strict_string_list(row.get("artifact_refs"), f"resolutions[{index}].artifact_refs")
                ),
            }
        )
    if seen != expected:
        raise ValueError("primary integration does not resolve every question")
    return {
        "schema_version": 1,
        "question_pack_id": question_pack.pack_id,
        "secondary_evidence_hash": secondary_evidence_hash,
        "resolutions": normalized,
    }


def _post_integration_artifact_evidence(
    integration: Mapping[str, object],
    artifact_root: str | Path,
    original_report: CompletenessReport,
) -> tuple[dict[str, str], ...] | None:
    try:
        root = Path(artifact_root).resolve()
    except OSError:
        return None
    rows = integration.get("resolutions")
    if not isinstance(rows, list):
        return None
    integrated: dict[str, PlanningArtifact] = {}
    for row in rows:
        if not isinstance(row, dict):
            return None
        kind = row.get("artifact_kind")
        refs = row.get("artifact_refs")
        if kind not in PLANNING_KINDS or not isinstance(refs, list) or not refs:
            return None
        for ref in refs:
            if not isinstance(ref, str) or not ref.strip():
                return None
            relative = Path(ref)
            if relative.is_absolute() or ".." in relative.parts:
                return None
            try:
                unresolved = root / relative
                if unresolved.is_symlink():
                    return None
                path = unresolved.resolve()
                path.relative_to(root)
                if path.is_symlink() or not path.is_file():
                    return None
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError, ValueError):
                return None
            artifact = PlanningArtifact(kind=str(kind), ref=ref, text=text)
            if not assess_planning_artifact(artifact).accepted:
                return None
            integrated[ref] = artifact

    post_integration: dict[str, PlanningArtifact] = {}
    for assessment in original_report.assessments:
        original = assessment.artifact
        relative = Path(original.ref)
        if relative.is_absolute() or ".." in relative.parts:
            return None
        replacement = integrated.get(original.ref)
        if replacement is not None:
            post_integration[original.ref] = PlanningArtifact(
                kind=original.kind,
                ref=original.ref,
                text=replacement.text,
            )
            continue
        unresolved = root / relative
        try:
            if unresolved.is_symlink() or not unresolved.is_file():
                return None
            resolved = unresolved.resolve()
            resolved.relative_to(root)
            post_integration[original.ref] = PlanningArtifact(
                kind=original.kind,
                ref=original.ref,
                text=resolved.read_text(encoding="utf-8"),
            )
        except (OSError, UnicodeDecodeError, ValueError):
            return None
    for ref, artifact in integrated.items():
        post_integration.setdefault(ref, artifact)
    final_artifacts: list[PlanningArtifact] = []
    artifact_evidence: list[dict[str, str]] = []
    for artifact in sorted(post_integration.values(), key=lambda item: (item.kind, item.ref)):
        try:
            relative = Path(artifact.ref)
            unresolved = root / relative
            if unresolved.is_symlink():
                return None
            resolved = unresolved.resolve()
            resolved.relative_to(root)
            content = resolved.read_bytes()
            text = content.decode("utf-8")
        except (OSError, UnicodeDecodeError, ValueError):
            return None
        final_artifacts.append(PlanningArtifact(kind=artifact.kind, ref=artifact.ref, text=text))
        artifact_evidence.append(
            {
                "kind": artifact.kind,
                "ref": artifact.ref,
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    if not assess_planning_completeness(final_artifacts).complete:
        return None
    return tuple(artifact_evidence)


@dataclass(frozen=True)
class PlanningGateRefs:
    brainstorm_peer: GateEvidenceRef | None = None
    foreign_review: GateEvidenceRef | None = None
    copilot: GateEvidenceRef | None = None

    def __post_init__(self) -> None:
        expected = (
            ("brainstorm_peer", self.brainstorm_peer, "brainstorm"),
            ("foreign_review", self.foreign_review, "foreign-review"),
            ("copilot", self.copilot, "copilot"),
        )
        refs: list[str] = []
        for field, value, kind in expected:
            if value is not None and (not isinstance(value, GateEvidenceRef) or value.kind != kind):
                raise ValueError(f"planning gate {field} 必須使用 {kind} kind")
            if value is not None:
                refs.append(value.ref)
        if len(refs) != len(set(refs)):
            raise ValueError("planning gate refs must be distinct")

    def as_tuple(self) -> tuple[GateEvidenceRef, ...]:
        return tuple(
            item for item in (self.brainstorm_peer, self.foreign_review, self.copilot) if item is not None
        )


@dataclass(frozen=True)
class BrainstormResult:
    state: str
    reason: str | None
    secondary_domain: str | None
    gate_refs: PlanningGateRefs
    integration: Mapping[str, object] | None = None


def _write_immutable_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (_canonical_json(payload) + "\n").encode("utf-8")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        try:
            if path.is_symlink() or not path.is_file() or path.read_bytes() != content:
                raise FileExistsError(f"conflicting immutable evidence: {path}") from exc
        except OSError as read_exc:
            raise FileExistsError(f"conflicting immutable evidence: {path}") from read_exc
        return
    try:
        with os.fdopen(fd, "wb", closefd=True) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def run_heterogeneous_brainstorm(
    *,
    report: CompletenessReport,
    primary: tuple[str, str],
    registry: IdentityRegistry,
    probes: Mapping[tuple[str, str], CapabilityProbe],
    evidence_dir: str | Path,
    artifact_root: str | Path,
    scope: PlanningScope,
    primary_questioner: Callable[[Mapping[str, object]], object],
    secondary_planner: Callable[[Mapping[str, object], ModelIdentity], object],
    primary_integrator: Callable[[Mapping[str, object], Mapping[str, object]], object],
) -> BrainstormResult:
    empty_refs = PlanningGateRefs()
    if report.complete:
        return BrainstormResult("ready", None, None, empty_refs, None)
    selection = select_secondary_planner(registry=registry, primary=primary, probes=probes)
    if selection.state != "ready" or selection.identity is None:
        return BrainstormResult("needs_human", selection.reason, None, empty_refs, None)
    try:
        pack = validate_question_pack(primary_questioner(report.to_dict()), report=report)
    except Exception:
        return BrainstormResult("needs_human", "question-pack-malformed", None, empty_refs, None)
    try:
        secondary = validate_secondary_evidence(
            secondary_planner(pack.to_dict(), selection.identity),
            question_pack=pack,
        )
    except Exception:
        return BrainstormResult(
            "needs_human",
            "secondary-output-malformed",
            selection.identity.independence_domain,
            empty_refs,
            None,
        )
    secondary_payload = secondary.to_dict()
    evidence_hash = _hash_payload(secondary_payload)
    callback_payload = {**secondary_payload, "evidence_hash": evidence_hash}
    try:
        integration = _validate_primary_integration(
            primary_integrator(pack.to_dict(), callback_payload),
            question_pack=pack,
            secondary_evidence_hash=evidence_hash,
        )
    except Exception:
        return BrainstormResult(
            "needs_human",
            "primary-integration-malformed",
            selection.identity.independence_domain,
            empty_refs,
            None,
        )
    artifact_evidence = _post_integration_artifact_evidence(integration, artifact_root, report)
    if artifact_evidence is None:
        return BrainstormResult(
            "needs_human",
            "primary-artifact-invalid",
            selection.identity.independence_domain,
            empty_refs,
            None,
        )
    evidence_payload = {
        "schema_version": BRAINSTORM_EVIDENCE_SCHEMA_VERSION,
        "kind": "brainstorm-peer",
        "scope": scope.to_dict(),
        "question_pack": pack.to_dict(),
        "secondary_identity": selection.identity.legacy_dict(),
        "secondary_evidence": secondary_payload,
        "secondary_evidence_hash": evidence_hash,
        "primary_integration": integration,
        "artifacts": list(artifact_evidence),
    }
    evidence_key = _hash_payload(
        {
            "scope": scope.to_dict(),
            "question_pack_id": pack.pack_id,
        }
    )[:32]
    evidence_path = Path(evidence_dir) / f"brainstorm-{evidence_key}.json"
    try:
        _write_immutable_json(evidence_path, evidence_payload)
    except FileExistsError:
        return BrainstormResult(
            "needs_human",
            "brainstorm-evidence-conflict",
            selection.identity.independence_domain,
            empty_refs,
            None,
        )
    except OSError:
        return BrainstormResult(
            "needs_human",
            "brainstorm-evidence-write-failed",
            selection.identity.independence_domain,
            empty_refs,
            None,
        )
    refs = PlanningGateRefs(
        brainstorm_peer=GateEvidenceRef(kind="brainstorm", ref=str(evidence_path))
    )
    return BrainstormResult(
        "ready",
        None,
        selection.identity.independence_domain,
        refs,
        integration,
    )
