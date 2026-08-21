from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import constants

REQUEST_TYPES = frozenset(
    {"tick", "fanout", "dispatch", "complete", "slice-action", "workflow-action", "work-action"}
)
WORK_ACTIONS = frozenset(
    {
        "link", "unlink", "start", "resume", "retry-build", "retry-card",
        "retry-verify", "retry-review", "recover-planning", "recover-pre-candidate",
        "recover-repair-commit", "regenerate-gates", "abandon", "retire-delivered",
        "recover-superseded",
        "reset-reclaim-budget", "refreeze-base", "auto", "ship", "review-attest",
        "intake",
    }
)
WORK_SOURCE_KINDS = frozenset({"github_issue", "github_pr", "openspec", "path"})


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def generate_req_id() -> str:
    return f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex}"


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target.parent / f".{target.name}.{uuid4().hex}.tmp"
    try:
        with temp_path.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.rename(temp_path, target)
    except BaseException:
        if temp_path.exists():
            temp_path.unlink()
        raise


def read_json(path: Path) -> dict[str, Any] | None:
    target = Path(path)
    if not target.exists():
        return None
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None


def build_request(*, req_type: str, args: dict[str, Any], requested_by: str) -> dict[str, Any]:
    if req_type not in REQUEST_TYPES:
        raise ValueError(f"unsupported request type: {req_type}")
    return {
        "schema_version": constants.SCHEMA_VERSION,
        "req_id": generate_req_id(),
        "type": req_type,
        "args": dict(args),
        "requested_by": requested_by,
        "created_at": utcnow(),
    }


def validate_request(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("schema_version") != constants.SCHEMA_VERSION:
        raise ValueError(
            f"invalid schema_version: expected {constants.SCHEMA_VERSION}, got {payload.get('schema_version')!r}"
        )
    req_id = payload.get("req_id")
    if not isinstance(req_id, str) or not req_id:
        raise ValueError("request req_id must be a non-empty string")
    req_type = payload.get("type")
    if req_type not in REQUEST_TYPES:
        raise ValueError(f"unsupported request type: {req_type!r}")
    args = payload.get("args")
    if not isinstance(args, dict):
        raise ValueError("request args must be an object")
    if req_type == "work-action":
        action = args.get("action")
        repo = args.get("repo")
        work_id = args.get("work_id")
        if action not in WORK_ACTIONS:
            raise ValueError("work-action action invalid")
        if not isinstance(repo, str) or not repo.strip():
            raise ValueError("work-action repo required")
        if not isinstance(work_id, str) or not work_id.strip():
            raise ValueError("work-action work_id required")
        if action in {"link", "unlink"}:
            issue = args.get("issue")
            kind = args.get("kind")
            ref = args.get("ref")
            legacy = isinstance(issue, int) and not isinstance(issue, bool) and issue > 0
            typed = kind in WORK_SOURCE_KINDS and isinstance(ref, str) and bool(ref.strip())
            if legacy == typed:
                raise ValueError("work-action link requires exactly one of --issue or --kind/--ref")
        if action == "intake":
            # issue #203：intake 合成「（必要時）link + start」——issue/kind+ref
            # 在 intake 是可省略的（work_id 已有 confirmed authority 時不需再帶），
            # 但一旦帶了任何一個欄位，就必須符合與 link 相同的恰好擇一語法，
            # 否則同樣 fail-closed（複用上面 link/unlink 的 legacy^typed 判準）。
            issue = args.get("issue")
            kind = args.get("kind")
            ref = args.get("ref")
            provided = issue is not None or kind is not None or ref is not None
            legacy = isinstance(issue, int) and not isinstance(issue, bool) and issue > 0
            typed = kind in WORK_SOURCE_KINDS and isinstance(ref, str) and bool(ref.strip())
            if provided and legacy == typed:
                raise ValueError(
                    "work-action intake 若帶 issue/kind+ref，必須恰好擇一（或兩者皆不帶）"
                )
        if action in {
            "retry-build", "retry-verify", "retry-review", "recover-repair-commit",
        } and (
            not isinstance(args.get("expected_candidate"), str)
            or re.fullmatch(r"[0-9a-fA-F]{40}", args["expected_candidate"]) is None
        ):
            raise ValueError(f"work-action {action} requires exact expected_candidate")
        if action in {"start", "intake"} and "combo" in args:
            combo = args.get("combo")
            if (
                not isinstance(combo, str)
                or re.fullmatch(r"[a-z0-9][a-z0-9-]*", combo) is None
            ):
                raise ValueError(f"work-action {action} combo invalid")
        if action not in {"start", "intake"} and "combo" in args:
            # combo override 只在 start／intake 有意義（intake 內部等價於
            # start，見 --combo 說明）；其餘 action 夾帶 combo 一律 fail-closed
            # 拒絕，contract 是所有入口的收斂點，防止未經驗證的 combo 被
            # resume 等動作間接餵進 start_canonical_workflow（見 code review
            # finding：coordinator/cli.py、porcelain/run.py、
            # coordinator/manager.py 三處縱深防禦）。
            raise ValueError(f"work-action {action} must not include combo")
        if action == "recover-planning":
            expected_run_id = args.get("expected_run_id")
            failure_classification = args.get("failure_classification")
            failure_reason = args.get("failure_reason")
            if (
                not isinstance(expected_run_id, str)
                or re.fullmatch(r"workflow-[0-9a-f]{20}", expected_run_id) is None
            ):
                raise ValueError("work-action recover-planning requires exact expected_run_id")
            if (
                not isinstance(failure_classification, str)
                or failure_classification not in {"environment", "content"}
            ):
                raise ValueError("work-action recover-planning requires failure_classification")
            if (
                not isinstance(failure_reason, str)
                or not failure_reason.strip()
                or "\n" in failure_reason
            ):
                raise ValueError("work-action recover-planning requires failure_reason")
        if action in {"recover-repair-commit", "regenerate-gates", "retry-card"}:
            # #540：regenerate-gates 重跑 gate 的對象是一個具體的 WorkflowRun，
            # 與 recover-repair-commit 同樣以 exact run CAS 定錨，避免在 operator
            # 心裡想的 run 之外的 run 上動手。#545：retry-card 重派一張卡同理。
            expected_run_id = args.get("expected_run_id")
            if (
                not isinstance(expected_run_id, str)
                or re.fullmatch(r"workflow-[0-9a-f]{20}", expected_run_id) is None
            ):
                raise ValueError(
                    f"work-action {action} requires exact expected_run_id"
                )
        if action == "retry-card":
            # #545：run CAS 之外還要指名卡片——重派的對象是「run 內的某一張卡」，
            # 少了卡名就不是精確定錨。合法性（是不是 builder 卡、是不是下一張要
            # 派的卡、有沒有已採信的 evidence）由 work action fail-closed 驗，
            # 這裡只做語法界限。
            card = args.get("card")
            if (
                not isinstance(card, str)
                or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", card) is None
            ):
                raise ValueError("work-action retry-card requires exact card id")
        if action in {"abandon", "retire-delivered", "recover-superseded"}:
            expected_run_id = args.get("expected_run_id")
            actor = args.get("actor")
            reason = args.get("reason")
            if (
                not isinstance(expected_run_id, str)
                or re.fullmatch(r"workflow-[0-9a-f]{20}", expected_run_id) is None
            ):
                raise ValueError(f"work-action {action} requires exact expected_run_id")
            if (
                not isinstance(actor, str)
                or actor != actor.strip()
                or not 1 <= len(actor) <= 128
                or not actor.isprintable()
            ):
                raise ValueError(f"work-action {action} requires bounded actor")
            if (
                not isinstance(reason, str)
                or reason != reason.strip()
                or not 1 <= len(reason) <= 500
                or not reason.isprintable()
            ):
                raise ValueError(f"work-action {action} requires bounded reason")
        if action == "refreeze-base":
            # issue #731 (A)：明示把候選 git base 重新凍結到目前的 `origin/main`。
            # 界限刻意與 abandon／retire-delivered／reset-reclaim-budget 同一族
            # （同樣是「一個人明示把一個已凍結的事實推到新值」）：bounded actor ＋
            # bounded reason。與 reclaim-reset 的差別是這裡**要** expected_run_id
            # ——重新凍結的對象就是一個具體的、還活著的 WorkflowRun，少了 exact
            # CAS 就可能動到 operator 心裡想的那個 run 之外的 run。
            expected_run_id = args.get("expected_run_id")
            actor = args.get("actor")
            reason = args.get("reason")
            if (
                not isinstance(expected_run_id, str)
                or re.fullmatch(r"workflow-[0-9a-f]{20}", expected_run_id) is None
            ):
                raise ValueError(f"work-action {action} requires exact expected_run_id")
            if (
                not isinstance(actor, str)
                or actor != actor.strip()
                or not 1 <= len(actor) <= 128
                or not actor.isprintable()
            ):
                raise ValueError(f"work-action {action} requires bounded actor")
            if (
                not isinstance(reason, str)
                or reason != reason.strip()
                or not 1 <= len(reason) <= 500
                or not reason.isprintable()
            ):
                raise ValueError(f"work-action {action} requires bounded reason")
        if action == "reset-reclaim-budget":
            # issue #519：明示重置 semantic-reclaim 世代熔斷。actor／reason 的
            # 界限與 abandon／retire-delivered 完全一致（同樣是「一個人明示解
            # 除一道安全機制」）；差別只在不要 expected_run_id——熔斷觸發的前
            # 提就是沒有 active run 可供 CAS。
            actor = args.get("actor")
            reason = args.get("reason")
            if (
                not isinstance(actor, str)
                or actor != actor.strip()
                or not 1 <= len(actor) <= 128
                or not actor.isprintable()
            ):
                raise ValueError(f"work-action {action} requires bounded actor")
            if (
                not isinstance(reason, str)
                or reason != reason.strip()
                or not 1 <= len(reason) <= 500
                or not reason.isprintable()
            ):
                raise ValueError(f"work-action {action} requires bounded reason")
    requested_by = payload.get("requested_by")
    if not isinstance(requested_by, str) or not requested_by:
        raise ValueError("request requested_by must be a non-empty string")
    created_at = payload.get("created_at")
    if not isinstance(created_at, str) or not created_at:
        raise ValueError("request created_at must be a non-empty string")
    return {
        "schema_version": constants.SCHEMA_VERSION,
        "req_id": req_id,
        "type": req_type,
        "args": dict(args),
        "requested_by": requested_by,
        "created_at": created_at,
    }


def build_done(
    *,
    req_id: str,
    status: str,
    result: dict[str, Any] | None = None,
    error: str | None = None,
    started_at: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": constants.SCHEMA_VERSION,
        "req_id": req_id,
        "status": status,
        "result": result,
        "error": error,
        "started_at": started_at,
        "finished_at": utcnow(),
    }


def build_status(
    *,
    ready: list[Any],
    in_flight: list[dict[str, Any]],
    recent_done: list[dict[str, Any]],
    daemon: dict[str, Any],
    updated_at: str,
) -> dict[str, Any]:
    return {
        "schema_version": constants.SCHEMA_VERSION,
        "updated_at": updated_at,
        "daemon": daemon,
        "ready": list(ready),
        "in_flight": list(in_flight),
        "recent_done": list(recent_done),
    }
