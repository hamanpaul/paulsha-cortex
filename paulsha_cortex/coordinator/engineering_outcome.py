"""#275：canonical engineering outcome contract，供外部 learning systems 消費。

本模組是「一個 work item 的工程結果最終落在哪裡」這件事的單一真相源。設計依據見
``docs/superpowers/specs/engineering-outcome-contract-{spec,design}.md``：

- outcome 詞彙縮限：``WorkflowRun.status`` 現況只有 ``ongoing``／``done``／
  ``superseded`` 三個合法值（見 ``registry.py``），run 級沒有 ``failed``／
  ``rejected``／``rolled_back`` 的既有終局轉換點。:data:`OUTCOME_STATUSES`
  因此列出六種 schema 合法值供未來擴張，但 v1 只有 ``shipped``（對應
  ``_ship_action`` 的 ``status="done"`` 轉換）與 ``abandoned``（對應
  ``_abandon_action`` 的 ``status="superseded"`` 轉換）兩種實際 emitter；其餘
  三種是預留值，尚無呼叫端會產生。
- idempotency：:func:`outcome_id` 由 ``run_id``／``outcome``／``attempt_digest``
  決定性推導；呼叫端傳入同一次終局轉換的內容位址 digest（例如 ship 的
  completion record hash、abandon 的 evidence digest），daemon 重跑或 request
  retry 只要落在同一次轉換上就會產生相同 id，:meth:`OutcomeStore.append`
  據此去重，不會產生第二筆 record。
- ``execution_provenance.session_refs``：Cortex job record 目前沒有存 executor
  自身的 session UUID（只有 ``session_name``、``log_path``、``pane``），因此
  這裡只能提供 worktree-path＋時間窗的弱 correlation hint，不宣稱 exact
  session match；捕捉真正的 executor session id 是 dispatcher.py 的另一項
  變更，超出本模組範圍。

本模組刻意維持純函式／純資料，不 import manager、registry 或 work_actions；
呼叫端負責在既有終局轉換點呼叫 :func:`emit_outcome`。本模組也不 import 任何
hippo 套件——這是外部 learning systems（含 Hippo）消費的唯讀 outbox，Hippo
未安裝時本模組的一切行為必須維持不變。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Iterable, Iterator, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from paulsha_cortex.config import paths

# canonical envelope 的 kind／版本。
ENGINEERING_OUTCOME_KIND = "cortex/engineering-outcome/v1"
ENGINEERING_OUTCOME_SCHEMA_VERSION = 1

# schema 合法值全集。v1 只有 shipped／abandoned 有 emitter（見模組 docstring）；
# rejected／failed／rolled_back 是保留值，供未來擴張既有終局轉換點時沿用同一
# schema，不需要消費端另外處理新 kind。
OUTCOME_STATUSES = (
    "shipped",
    "abandoned",
    "rejected",
    "failed",
    "rolled_back",
)

# 目前實際會被 emit 的子集（見 work_actions._ship_action／_abandon_action）。
EMITTED_OUTCOME_STATUSES = ("shipped", "abandoned")

OUTCOME_ID_PATTERN = re.compile(r"outcome-[0-9a-f]{20}")
WORKFLOW_RUN_ID_PATTERN = re.compile(r"workflow-[0-9a-f]{20}")

_RECORD_MAPPING_FIELDS = ("candidate", "verification", "review", "execution_provenance")
_JOB_STRING_FIELDS = ("card", "persona", "workflow_phase")


class EngineeringOutcomeError(ValueError):
    """確定性的 engineering outcome 契約違規；一律 fail closed，且錯誤可被機器讀取。"""

    def __init__(
        self,
        message: str,
        *,
        reason: str,
        validation_path: str = "$",
        errors: tuple[dict[str, Any], ...] = (),
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.validation_path = validation_path
        self.errors = errors

    def as_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "validation_path": self.validation_path,
            "errors": [dict(item) for item in self.errors],
            "message": str(self),
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fsync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _repo_slug(repo: str) -> str:
    if not isinstance(repo, str) or not repo:
        raise EngineeringOutcomeError(
            "engineering outcome repo 必須為非空字串",
            reason="repo-missing",
            validation_path="$.repo",
        )
    slug = re.sub(r"[^a-z0-9]+", "-", repo.lower()).strip("-")
    if not slug:
        raise EngineeringOutcomeError(
            f"engineering outcome repo 無法推導出檔名 slug：{repo!r}",
            reason="repo-slug-invalid",
            validation_path="$.repo",
        )
    return slug


def outcome_id(*, run_id: str, outcome: str, attempt_digest: str) -> str:
    """由 run_id／outcome／attempt_digest 決定性推導 outcome_id（供 idempotency 判定）。

    同一次終局轉換的重複 tick（daemon restart、request retry）只要傳入相同的
    ``attempt_digest``（呼叫端應使用該次轉換本身的內容位址 digest，例如
    completion record hash 或 abandon evidence digest）就會得到相同 id，讓
    :meth:`OutcomeStore.append` 據此去重。
    """

    if not isinstance(run_id, str) or WORKFLOW_RUN_ID_PATTERN.fullmatch(run_id) is None:
        raise EngineeringOutcomeError(
            f"outcome_id 需要合法 run_id：{run_id!r}",
            reason="workflow-run-id-invalid",
            validation_path="$.workflow_run_id",
        )
    if outcome not in OUTCOME_STATUSES:
        raise EngineeringOutcomeError(
            f"outcome_id 收到非法 outcome：{outcome!r}",
            reason="outcome-status-invalid",
            validation_path="$.outcome",
            errors=({"observed": outcome, "expected": list(OUTCOME_STATUSES)},),
        )
    if not isinstance(attempt_digest, str) or not attempt_digest:
        raise EngineeringOutcomeError(
            "outcome_id 需要非空 attempt_digest",
            reason="attempt-digest-invalid",
            validation_path="$.outcome_id",
        )
    digest = hashlib.sha256(
        f"{run_id}|{outcome}|{attempt_digest}".encode("utf-8")
    ).hexdigest()
    return f"outcome-{digest[:20]}"


def _project_jobs(jobs: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """把原始 job record 收斂成 outcome 契約要公開的欄位（#275 notes 3b）：

    ``job_id``／``card``（＝job 的 ``workflow_card``）／``persona``／
    ``workflow_phase``——這幾個欄位 job record 本來就有，公開成本低，因此展開
    成 per-job 物件而非扁平字串陣列。
    """

    projected: list[dict[str, Any]] = []
    for job in jobs:
        job_id = job.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            continue
        projected.append(
            {
                "job_id": job_id,
                "card": job.get("workflow_card"),
                "persona": job.get("persona"),
                "workflow_phase": job.get("workflow_phase"),
            }
        )
    projected.sort(key=lambda row: row["job_id"])
    return projected


def _derive_slice_id(jobs: Iterable[Mapping[str, Any]]) -> str | None:
    """slice 身分＝dispatch task（見 ``registry.create_job`` 的 ``task`` 參數，
    也是 ``job_id`` 的字首與 ``feature/<task>`` 分支的來源）。一個 workflow run
    底下的 job 理應共用同一個 task；若觀察到不一致（不應該發生，但唯讀 surface
    fail-soft 優先於整段失敗）就不猜測，回 ``None``。
    """

    tasks = {
        job.get("task")
        for job in jobs
        if isinstance(job.get("task"), str) and job.get("task")
    }
    if len(tasks) == 1:
        return next(iter(tasks))
    return None


def _build_execution_provenance(*, run: Any, jobs: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """已知落差（見模組 docstring）：job record 沒有 executor 自身的 session
    UUID，只有 ``session_name``；因此這裡只給 worktree-path＋時間窗的弱
    correlation hint，``correlation_confidence`` 明確標示為 ``weak``，不宣稱
    exact session match。
    """

    session_refs = sorted(
        {
            job.get("session_name")
            for job in jobs
            if isinstance(job.get("session_name"), str) and job.get("session_name")
        }
    )
    return {
        "worktree_root": run.workspace_root,
        "time_window": {"started_at": run.created_at, "observed_at": run.updated_at},
        "session_refs": session_refs,
        "correlation_confidence": "weak",
    }


def build_outcome_record(
    *,
    run: Any,
    authority: Any,
    jobs: Sequence[Mapping[str, Any]],
    outcome: str,
    attempt_digest: str,
    candidate: Mapping[str, Any] | None = None,
    reason_code: str | None = None,
    verification: Mapping[str, Any] | None = None,
    review: Mapping[str, Any] | None = None,
    emitted_at: str | None = None,
    supersedes_outcome_id: str | None = None,
) -> dict[str, Any]:
    """組出一筆 canonical engineering outcome record 並驗證後回傳。

    ``run`` 須為 :class:`paulsha_cortex.coordinator.workflow.WorkflowRun`，
    ``authority`` 須為 :class:`paulsha_cortex.coordinator.claim.WorkAuthority`；
    本函式只讀取兩者既有欄位，不做任何額外查詢。
    """

    record = {
        "schema": ENGINEERING_OUTCOME_KIND,
        "schema_version": ENGINEERING_OUTCOME_SCHEMA_VERSION,
        "outcome_id": outcome_id(run_id=run.run_id, outcome=outcome, attempt_digest=attempt_digest),
        "emitted_at": emitted_at or _now_iso(),
        "repo": authority.repo,
        "work_id": authority.work_id,
        "workflow_run_id": run.run_id,
        "slice_id": _derive_slice_id(jobs),
        "jobs": _project_jobs(jobs),
        "candidate": dict(candidate) if candidate else {},
        "outcome": outcome,
        "reason_code": reason_code,
        "verification": dict(verification) if verification else {},
        "review": dict(review) if review else {},
        "execution_provenance": _build_execution_provenance(run=run, jobs=jobs),
        "supersedes_outcome_id": supersedes_outcome_id,
    }
    return validate_outcome_record(record)


def validate_outcome_record(payload: object) -> dict[str, Any]:
    """驗證 canonical engineering outcome envelope；fail-closed，不做寬鬆解析。"""

    if not isinstance(payload, Mapping):
        raise EngineeringOutcomeError(
            f"engineering outcome payload 不是物件：{type(payload).__name__}",
            reason="payload-not-object",
        )
    schema = payload.get("schema")
    if schema != ENGINEERING_OUTCOME_KIND:
        raise EngineeringOutcomeError(
            f"engineering outcome schema 非法：{schema!r}",
            reason="schema-unknown",
            validation_path="$.schema",
            errors=({"observed": schema, "expected": ENGINEERING_OUTCOME_KIND},),
        )
    schema_version = payload.get("schema_version")
    if schema_version != ENGINEERING_OUTCOME_SCHEMA_VERSION:
        raise EngineeringOutcomeError(
            f"engineering outcome schema_version 不受支援：{schema_version!r}",
            reason="schema-version-unsupported",
            validation_path="$.schema_version",
            errors=(
                {
                    "observed": schema_version,
                    "expected": ENGINEERING_OUTCOME_SCHEMA_VERSION,
                },
            ),
        )
    outcome_id_value = payload.get("outcome_id")
    if not isinstance(outcome_id_value, str) or OUTCOME_ID_PATTERN.fullmatch(outcome_id_value) is None:
        raise EngineeringOutcomeError(
            f"engineering outcome outcome_id 格式非法：{outcome_id_value!r}",
            reason="outcome-id-invalid",
            validation_path="$.outcome_id",
        )
    emitted_at = payload.get("emitted_at")
    if not isinstance(emitted_at, str) or not emitted_at:
        raise EngineeringOutcomeError(
            "engineering outcome 缺少 emitted_at",
            reason="emitted-at-missing",
            validation_path="$.emitted_at",
        )
    for field_name in ("repo", "work_id"):
        value = payload.get(field_name)
        if not isinstance(value, str) or not value:
            raise EngineeringOutcomeError(
                f"engineering outcome 缺少 {field_name}",
                reason=f"{field_name}-missing",
                validation_path=f"$.{field_name}",
            )
    run_id = payload.get("workflow_run_id")
    if not isinstance(run_id, str) or WORKFLOW_RUN_ID_PATTERN.fullmatch(run_id) is None:
        raise EngineeringOutcomeError(
            f"engineering outcome workflow_run_id 格式非法：{run_id!r}",
            reason="workflow-run-id-invalid",
            validation_path="$.workflow_run_id",
        )
    outcome = payload.get("outcome")
    if outcome not in OUTCOME_STATUSES:
        raise EngineeringOutcomeError(
            f"engineering outcome outcome 非法：{outcome!r}",
            reason="outcome-status-invalid",
            validation_path="$.outcome",
            errors=({"observed": outcome, "expected": list(OUTCOME_STATUSES)},),
        )
    slice_id = payload.get("slice_id")
    if slice_id is not None and (not isinstance(slice_id, str) or not slice_id):
        raise EngineeringOutcomeError(
            f"engineering outcome slice_id 型別錯誤：{slice_id!r}",
            reason="slice-id-invalid",
            validation_path="$.slice_id",
        )

    jobs_raw = payload.get("jobs")
    if not isinstance(jobs_raw, list):
        raise EngineeringOutcomeError(
            "engineering outcome jobs 必須為陣列",
            reason="jobs-invalid",
            validation_path="$.jobs",
        )
    jobs: list[dict[str, Any]] = []
    for index, item in enumerate(jobs_raw):
        if not isinstance(item, Mapping):
            raise EngineeringOutcomeError(
                "engineering outcome jobs 項目必須為物件",
                reason="jobs-invalid",
                validation_path=f"$.jobs[{index}]",
            )
        job_id = item.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            raise EngineeringOutcomeError(
                "engineering outcome job 缺少 job_id",
                reason="jobs-invalid",
                validation_path=f"$.jobs[{index}].job_id",
            )
        row: dict[str, Any] = {"job_id": job_id}
        for key in _JOB_STRING_FIELDS:
            value = item.get(key)
            if value is not None and not isinstance(value, str):
                raise EngineeringOutcomeError(
                    f"engineering outcome job {key} 型別錯誤",
                    reason="jobs-invalid",
                    validation_path=f"$.jobs[{index}].{key}",
                )
            row[key] = value
        jobs.append(row)

    for field_name in _RECORD_MAPPING_FIELDS:
        value = payload.get(field_name, {})
        if not isinstance(value, Mapping):
            raise EngineeringOutcomeError(
                f"engineering outcome {field_name} 必須為物件",
                reason=f"{field_name}-invalid",
                validation_path=f"$.{field_name}",
            )

    reason_code = payload.get("reason_code")
    if reason_code is not None and (not isinstance(reason_code, str) or not reason_code):
        raise EngineeringOutcomeError(
            f"engineering outcome reason_code 型別錯誤：{reason_code!r}",
            reason="reason-code-invalid",
            validation_path="$.reason_code",
        )

    supersedes = payload.get("supersedes_outcome_id")
    if supersedes is not None and (
        not isinstance(supersedes, str) or OUTCOME_ID_PATTERN.fullmatch(supersedes) is None
    ):
        raise EngineeringOutcomeError(
            f"engineering outcome supersedes_outcome_id 格式非法：{supersedes!r}",
            reason="supersedes-outcome-id-invalid",
            validation_path="$.supersedes_outcome_id",
        )

    return {
        "schema": schema,
        "schema_version": schema_version,
        "outcome_id": outcome_id_value,
        "emitted_at": emitted_at,
        "repo": payload["repo"],
        "work_id": payload["work_id"],
        "workflow_run_id": run_id,
        "slice_id": slice_id,
        "jobs": jobs,
        "candidate": dict(payload.get("candidate") or {}),
        "outcome": outcome,
        "reason_code": reason_code,
        "verification": dict(payload.get("verification") or {}),
        "review": dict(payload.get("review") or {}),
        "execution_provenance": dict(payload.get("execution_provenance") or {}),
        "supersedes_outcome_id": supersedes,
    }


class OutcomeStore:
    """單一 repo 的 append-only engineering outcome outbox（一 repo 一個 JSONL 檔）。

    選擇一 repo 一檔而非一 work_id 一檔：後者會隨 work item 數量開出大量小檔，
    每次 append 都要各自 fsync，長期造成 fsync 爆量；前者把同一 repo 的所有
    outcome 收斂進同一份可 tail 的 JSONL，append 頻率與終局轉換次數同數量級
    （ship／abandon 本身就不頻繁）。

    每次 :meth:`append` 都整檔讀回、去重、整檔以 tempfile+``os.replace`` 重寫
    ——與 ``registry.JobRegistry._write_payload_atomically`` 同一套「寫暫存檔→
    fsync→原子換位→fsync 目錄」手法，只是這裡持有的是逐行 JSON 而非單一
    JSON 文件。單一 outcome record 量體小且事件頻率低，整檔重寫不是效能瓶頸；
    換取的是不需要另外維護索引檔就能做 idempotency 檢查。
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def _read_records(self) -> list[dict[str, Any]]:
        if not self._path.is_file():
            return []
        records: list[dict[str, Any]] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            records.append(json.loads(line))
        return records

    def _write_all(self, records: list[dict[str, Any]]) -> None:
        directory = self._path.parent
        directory.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(directory), suffix=".tmp")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                for row in records:
                    handle.write(
                        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                    )
                    handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self._path)
            _fsync_directory(directory)
        finally:
            tmp.unlink(missing_ok=True)

    def append(self, record: Mapping[str, Any]) -> dict[str, Any]:
        """驗證並寫入一筆 outcome record；同 ``outcome_id`` 已存在時直接回傳既有
        record，不重複 append（daemon restart／request retry 的重複 tick 因此不
        會產生第二筆）。
        """

        validated = validate_outcome_record(record)
        records = self._read_records()
        for existing in records:
            if existing.get("outcome_id") == validated["outcome_id"]:
                return existing
        records.append(validated)
        self._write_all(records)
        return validated

    def list_outcomes(
        self, *, repo: str | None = None, work_id: str | None = None
    ) -> Iterator[dict[str, Any]]:
        for row in self._read_records():
            if repo is not None and row.get("repo") != repo:
                continue
            if work_id is not None and row.get("work_id") != work_id:
                continue
            yield row

    def show_outcome(self, outcome_id_value: str) -> dict[str, Any] | None:
        for row in self._read_records():
            if row.get("outcome_id") == outcome_id_value:
                return row
        return None

    def replay_outcomes(self, *, since: str | None = None) -> Iterator[dict[str, Any]]:
        for row in self._read_records():
            if since is not None and str(row.get("emitted_at")) < since:
                continue
            yield row


def default_outcomes_root() -> Path:
    return paths.coordinator_root() / "engineering-outcomes"


def outcome_store_path_for_repo(root: Path, repo: str) -> Path:
    return Path(root) / f"{_repo_slug(repo)}.jsonl"


def outcome_store_path(state_path: str | Path, *, repo: str) -> Path:
    """由呼叫端既有的 delivery-journal ``state_path`` 推導 outcome store 路徑
    （比照 ``work_actions._ship_binding`` 等既有 evidence 目錄的 sibling
    pattern：``state_path.resolve().parent / "evidence" / ...``，這裡是
    ``.../"engineering-outcomes"/<repo-slug>.jsonl``）。
    """

    root = Path(state_path).resolve().parent / "engineering-outcomes"
    return outcome_store_path_for_repo(root, repo)


def emit_outcome(
    store: OutcomeStore,
    *,
    run: Any,
    authority: Any,
    jobs: Sequence[Mapping[str, Any]],
    outcome: str,
    attempt_digest: str,
    candidate: Mapping[str, Any] | None = None,
    reason_code: str | None = None,
    verification: Mapping[str, Any] | None = None,
    review: Mapping[str, Any] | None = None,
    emitted_at: str | None = None,
    supersedes_outcome_id: str | None = None,
) -> dict[str, Any]:
    """組出一筆 outcome record，驗證後 durable 寫入 ``store``。

    呼叫端（``work_actions._ship_action``／``_abandon_action``）MUST 在改動
    ``WorkflowRun.status`` 的終局轉換呼叫（``_manager_update_workflow_run``／
    ``_manager_abandon_workflow_run``）之前呼叫本函式，確保 outcome 先於
    terminal transition durable 落地。``jobs`` 只需傳入該 run 底下的全部 job
    record（例如 ``workflow_registry.list_jobs()`` 的結果），本函式會依
    ``workflow_run_id`` 過濾。
    """

    run_jobs = tuple(job for job in jobs if job.get("workflow_run_id") == run.run_id)
    record = build_outcome_record(
        run=run,
        authority=authority,
        jobs=run_jobs,
        outcome=outcome,
        attempt_digest=attempt_digest,
        candidate=candidate,
        reason_code=reason_code,
        verification=verification,
        review=review,
        emitted_at=emitted_at,
        supersedes_outcome_id=supersedes_outcome_id,
    )
    return store.append(record)


def iter_outcome_stores(root: str | Path) -> Iterator[OutcomeStore]:
    """列舉 ``root`` 下所有 repo 的 outcome store（唯讀 surface 跨 repo 掃描用）。"""

    directory = Path(root)
    if not directory.is_dir():
        return
    for path in sorted(directory.glob("*.jsonl")):
        yield OutcomeStore(path)


def _stores_for(root: str | Path, *, repo: str | None) -> list[OutcomeStore]:
    if repo is not None:
        return [OutcomeStore(outcome_store_path_for_repo(Path(root), repo))]
    return list(iter_outcome_stores(root))


def list_outcomes(
    root: str | Path, *, repo: str | None = None, work_id: str | None = None
) -> Iterator[dict[str, Any]]:
    """跨 store 唯讀列表；``repo`` 省略時掃描 ``root`` 下全部 repo 檔案。"""

    for store in _stores_for(root, repo=repo):
        yield from store.list_outcomes(repo=repo, work_id=work_id)


def show_outcome(
    root: str | Path, outcome_id_value: str, *, repo: str | None = None
) -> dict[str, Any] | None:
    """跨 store 唯讀查找單筆 outcome；``repo`` 省略時掃描全部 repo 檔案。"""

    for store in _stores_for(root, repo=repo):
        record = store.show_outcome(outcome_id_value)
        if record is not None:
            return record
    return None


def replay_outcomes(
    root: str | Path, *, repo: str | None = None, since: str | None = None
) -> Iterator[dict[str, Any]]:
    """跨 store 依 ``emitted_at`` 唯讀重播；``repo`` 省略時掃描全部 repo 檔案。"""

    for store in _stores_for(root, repo=repo):
        yield from store.replay_outcomes(since=since)
