"""#845 A04：受治理的 live receipt validator 封閉登記表。

`requirement_delivery._verify_live` 只把已通過 schema／hash／target／freshness／
authority／independence 檢查的 receipt 物件（`Mapping[str, Any]`）交給
`live_receipt_validator(receipt) -> bool`；這個函式不拿到 `evidence_root`，也不能
有任何副作用（不得觸發模型、merge、部署、關票）。本模組提供該 callable 的正式
production 實作：

- 以 ``receipt["kind"]`` 為 key 的封閉登記表，只認得本模組明確實作的 receipt kind；
  未登記的 kind 一律回傳 ``False``（fail-closed），不得因為看不懂就放行。
- 每個 kind 的實際內容放在 ``receipt["evidence"]``（同一份已被 sha256 綁定的
  receipt 內容，不再另外讀檔），並且必須綁定與 receipt 同一個
  ``receipt["target"]``（該欄位已由呼叫端驗證等於這條需求 mapping 的
  acceptance target／已載入 runtime 身分；見 `requirement_delivery._target_identity`
  與 `_verify_installed`）。無法綁定即拒絕。
- 目前支援兩種 kind：
  1. ``cortex/deployment-canary-qualification/v1``：`qualification/validate.py`
     既有 fail-closed 邏輯的 deployment-canary 產物，`candidate_sha`／wheel
     sha256 必須等於這條需求 claim 的 target。
  2. ``cortex/task-memory-live-canary/v1``：#857 task-memory canary evidence，
     content retrieval／各 delivery path 成功率須達門檻，且負例與跨 project
     檢查全部通過。

任何解析失敗、格式錯誤或內容不符都視為未驗證通過，回傳 ``False``（對應
`requirement_delivery` 的 ``failed`` gap），絕不因為 kind 不認得或內容壞掉而放行。
"""
from __future__ import annotations

import re
from typing import Any, Callable, Mapping

KIND_DEPLOYMENT_CANARY_QUALIFICATION = "cortex/deployment-canary-qualification/v1"
KIND_TASK_MEMORY_LIVE_CANARY = "cortex/task-memory-live-canary/v1"

# #857 task-memory canary 的門檻：與規格 R7（canary acceptance）一致——至少 5 次
# 嘗試、eligible authorized content retrieval 成功率至少 95%。
_MIN_SUCCESS_RATE = 0.95
_MIN_PATH_ATTEMPTS = 5
_MIN_CROSS_PROJECT_REPOS = 2
_REQUIRED_DELIVERY_PATHS = frozenset({"context-delivered", "snapshot-ready", "note-fetch"})
_REQUIRED_NEGATIVE_CASES = frozenset({"permission-denied", "cross-scope-rejection"})
_REPO_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,98}[A-Za-z0-9])?/"
    r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,98}[A-Za-z0-9])?$"
)


def _rate_matches(*, attempts: object, successes: object, reported_rate: object) -> bool:
    """驗證 attempts/successes/success_rate 三者互相一致，且達到門檻；型別不符即拒絕。"""
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < _MIN_PATH_ATTEMPTS:
        return False
    if isinstance(successes, bool) or not isinstance(successes, int) or successes < 0 or successes > attempts:
        return False
    if isinstance(reported_rate, bool) or not isinstance(reported_rate, (int, float)):
        return False
    computed = successes / attempts
    if abs(float(reported_rate) - computed) > 1e-6:
        return False
    return computed >= _MIN_SUCCESS_RATE


def _validate_deployment_canary_qualification(receipt: Mapping[str, Any]) -> bool:
    """deployment-canary `qualification.json` receipt：沿用 `qualification/validate.py`
    既有驗證邏輯，不另寫一套；只額外綁定 candidate_sha／wheel sha256 等於 target。"""
    target = receipt.get("target")
    evidence = receipt.get("evidence")
    if not isinstance(target, Mapping) or not isinstance(evidence, Mapping):
        return False
    candidate_sha = target.get("candidate_sha")
    wheel_sha256 = target.get("artifact_sha256")
    if not isinstance(candidate_sha, str) or not candidate_sha or not isinstance(wheel_sha256, str) or not wheel_sha256:
        return False
    if evidence.get("profile") != "deployment-canary":
        return False
    try:
        # repo 根目錄的 `qualification/` 不隨 paulsha_cortex wheel 一起發佈；
        # 未安裝時 lazy import 失敗一律 fail-closed，不得因缺套件而放行。
        from qualification import validate as qualification_validate
    except ImportError:
        return False
    try:
        qualification_validate.validate(
            evidence,
            candidate_sha=candidate_sha,
            wheel_sha256=wheel_sha256,
            bundle_sha256=None,
            evidence_root=None,
            require_release_profile=False,
            require_canary_profile=False,
        )
    except qualification_validate.ValidationError:
        return False
    except (TypeError, ValueError, KeyError):
        return False
    return True


def _validate_task_memory_live_canary(receipt: Mapping[str, Any]) -> bool:
    """#857 task-memory canary evidence：content retrieval／各 path 成功率達門檻，
    負例與跨 project 檢查通過，且綁定同一個 target（即同時期 #841 loaded runtime
    receipt 的 artifact digest／revision）；無法綁定即拒絕。"""
    target = receipt.get("target")
    evidence = receipt.get("evidence")
    if not isinstance(target, Mapping) or not isinstance(evidence, Mapping):
        return False
    if evidence.get("schema") != KIND_TASK_MEMORY_LIVE_CANARY or evidence.get("passed") is not True:
        return False
    # evidence 必須明確宣告與 receipt 同一個 target；型別不符或不相等都拒絕。
    if not isinstance(evidence.get("target"), Mapping) or dict(evidence["target"]) != dict(target):
        return False

    content_retrieval = evidence.get("content_retrieval")
    if not isinstance(content_retrieval, Mapping):
        return False
    if not _rate_matches(
        attempts=content_retrieval.get("attempts"),
        successes=content_retrieval.get("successes"),
        reported_rate=content_retrieval.get("success_rate"),
    ):
        return False

    paths = evidence.get("paths")
    if not isinstance(paths, Mapping) or set(paths) != _REQUIRED_DELIVERY_PATHS:
        return False
    for path_name in _REQUIRED_DELIVERY_PATHS:
        row = paths.get(path_name)
        if not isinstance(row, Mapping):
            return False
        if not _rate_matches(
            attempts=row.get("attempts"),
            successes=row.get("successes"),
            reported_rate=row.get("success_rate"),
        ):
            return False

    negative_controls = evidence.get("negative_controls")
    if not isinstance(negative_controls, list):
        return False
    observed_cases: set[str] = set()
    for row in negative_controls:
        if not isinstance(row, Mapping):
            return False
        case = row.get("case")
        if not isinstance(case, str) or not case or row.get("status") != "passed":
            return False
        observed_cases.add(case)
    if not _REQUIRED_NEGATIVE_CASES.issubset(observed_cases):
        return False

    cross_project = evidence.get("cross_project")
    if not isinstance(cross_project, list) or len(cross_project) < _MIN_CROSS_PROJECT_REPOS:
        return False
    observed_repos: set[str] = set()
    for row in cross_project:
        if not isinstance(row, Mapping):
            return False
        repo = row.get("repo")
        if not isinstance(repo, str) or _REPO_RE.fullmatch(repo) is None or row.get("status") != "passed":
            return False
        observed_repos.add(repo)
    if len(observed_repos) < _MIN_CROSS_PROJECT_REPOS:
        return False

    return True


_VALIDATORS: dict[str, Callable[[Mapping[str, Any]], bool]] = {
    KIND_DEPLOYMENT_CANARY_QUALIFICATION: _validate_deployment_canary_qualification,
    KIND_TASK_MEMORY_LIVE_CANARY: _validate_task_memory_live_canary,
}


def governed_live_receipt_validator(receipt: Mapping[str, Any]) -> bool:
    """`requirement_delivery` 唯一的 production `live_receipt_validator`。

    封閉登記表：只接受本模組明確登記且可機械驗證的 receipt kind；未知 kind、
    型別不符或任何解析例外都回傳 ``False``（fail-closed），不得因為看不懂或
    內部壞掉而放行。此函式讀取傳入的 ``receipt`` 內容，不觸發模型、merge、
    部署或關票，亦不讀取檔案系統或網路。
    """
    if not isinstance(receipt, Mapping):
        return False
    kind = receipt.get("kind")
    if not isinstance(kind, str):
        return False
    validator = _VALIDATORS.get(kind)
    if validator is None:
        return False
    try:
        return validator(receipt) is True
    except Exception:
        return False
