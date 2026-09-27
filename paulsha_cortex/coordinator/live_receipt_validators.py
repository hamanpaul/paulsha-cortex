"""#845 A04：受治理的 live receipt validator 封閉登記表。

`requirement_delivery._verify_live` 只把已通過 schema／hash／target／freshness／
authority／independence 檢查的 receipt 物件（`Mapping[str, Any]`）交給
`live_receipt_validator(receipt) -> bool`；這個函式每次呼叫只吃 receipt 本身，
不能有任何副作用（不得觸發模型、merge、部署、關票）。本模組提供該 callable 的正式
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
     sha256 必須等於這條需求 claim 的 target，且以 `require_canary_profile=True`
     驗證——外部 canary 身分（repository／work_id／issue，見
     ``receipt["canary_target"]``）與 receipt 綁定的 evidence 目錄（真正落地的
     檔案集合與逐檔 sha256）皆須存在並通過。
  2. ``cortex/task-memory-live-canary/v1``：#857 task-memory canary evidence，
     content retrieval／各 delivery path 成功率須達門檻，且負例與跨 project
     檢查全部通過。

任何解析失敗、格式錯誤或內容不符都視為未驗證通過，回傳 ``False``（對應
`requirement_delivery` 的 ``failed`` gap），絕不因為 kind 不認得或內容壞掉而放行。

## #845 對抗審查（第八輪）：evidence_root 與 checkout 外執行

`governed_live_receipt_validator`（單一全域 callable）已被 `make_governed_live_
receipt_validator` 工廠取代：

- BLOCKER：先前 deployment-canary 驗證固定傳 `require_release_profile=False`、
  `require_canary_profile=False`、`evidence_root=None`，只做 payload 結構檢查，
  完全沒有比對真正的 evidence 檔案集合與內容，只含 `fresh-install`／
  `full-dispatch-closeout` 加一個假 artifact 的 receipt 就會被採信。現在
  `_validate_deployment_canary_qualification` 一律以 `require_canary_profile=True`
  呼叫 `qualification/validate.py` 的 `validate()`，並要求 receipt 額外帶
  ``canary_target``（`repository`／`work_id`／`issue`／`evidence_directory`）：
  `repository` 必須等於已驗證的 `target["repo"]`；`evidence_directory` 是相對
  delivery `evidence_root`（呼叫端於工廠建立時傳入、非 receipt 內容可操控）的
  安全 locator，解析到的目錄交給 `validate()` 的 `evidence_root` 參數，實際核對
  每個宣告 artifact 的檔案存在、內容 sha256 與 artifact-inventory 逐檔一致、
  evidence tree 沒有多餘或缺漏檔案。任一項缺失、路徑不安全（絕對路徑、`..`、
  symlink）或內容不符，一律 fail closed。
- MAJOR：`from qualification import validate` 是一般 import，依賴 repo 根目錄的
  `qualification/` 剛好在 `sys.path` 上；wheel 只打包 `paulsha_cortex*`，已安裝
  的 `cortex delivery gaps` 在 checkout 外執行時，這個 import 一律 `ImportError`，
  導致合法 receipt 永遠被拒。現在改由工廠接收 CLI 已要求的 `--source-root`
  （checkout 根目錄），以 `importlib.util.spec_from_file_location` 從
  `<source_root>/qualification/validate.py` 動態載入；路徑須嚴格在 source_root
  之下且不得是 symlink，缺檔或載入失敗一律回傳 `None`，deployment-canary
  validator 遇到 `None` 立即 fail closed，不影響 task-memory kind。
  `porcelain/delivery.py` 以此工廠建立 `common["live_receipt_validator"]`。

## #845 對抗審查（第九輪）：canary_domain 自報與 target 欄位部分綁定

- BLOCKER（`requirement_delivery.py` 的 `_verify_live`）：`independence.
  canary_domain` 先前完全是 receipt 自報字串，同一個 reviewer 產生的 receipt
  只要把它改成任意不同於 reviewer domain 的值就能通過 independence 檢查。
  現在改由本模組的 `derive_canary_domain(receipt)` 依 `receipt["kind"]` 從
  **已通過 schema 檢查的 evidence** 導出，`_verify_live` 只用導出值；receipt
  若仍自報 `independence.canary_domain`，該值必須與導出值完全相同，否則拒絕
  （不是「有填就信」，是「填了就必須對得上」）。兩種已登記 kind 的導出來源：
  - `cortex/deployment-canary-qualification/v1`：導出來源是 evidence 的
    `image.digest`（`sha256:` + 64 hex）。`qualification/driver.py` 的
    docstring 明示這是「複製進候選掛載之前的參考映像」——與候選 checkout、
    builder／reviewer 執行環境無關，是這個 kind 的 schema 已經強制存在且
    格式受限的欄位，不是額外自由填的字串。
  - `cortex/task-memory-live-canary/v1`：導出來源是 evidence 的
    `executor.id`（非空字串）。這是本模組新增的必要欄位（原 schema 沒有任何
    執行環境／執行者身分可用於 independence 導出），代表產生這份 canary
    量測的執行者身分，與 review 執行環境無關。
  兩種來源都無法取得時（kind 未登記、evidence 型別不符、欄位缺失或格式錯誤）
  一律回傳 `None`，`_verify_live` 視為 independence 檢查失敗（fail closed），
  不得代入任何預設值放行。
- MAJOR（`_validate_deployment_canary_qualification`）：先前只把
  `candidate_sha`／`artifact_sha256`（進而透過 `canary_target.repository`
  綁 `repo`）與 acceptance target 比對；target 其餘五個欄位——
  `source_revision`／`service`／`instance`／`profile_key`／`config_revision`
  ——qualification evidence 的 schema完全沒有對應內容可比對（canary 測的是
  「這顆 wheel／candidate」本身能否通過 attack matrix 與基本 dispatch，不是
  某個特定 profile／instance 的部署事實），所以先前的程式碼就沒有檢查它們；
  同一份真實 canary evidence 因而可以在只換掉外層 `receipt["target"]` 這五個
  欄位後被重複拿去核銷不同 profile/config/instance 的需求，違反 A04 exact
  target。修法：**每種 kind 先明列 evidence 可證明的 target 欄位**——
  deployment-canary-qualification 只能證明 `repo`／`candidate_sha`／
  `artifact_sha256`（已直接比對 evidence 內容）；其餘五個欄位改用內容定址
  綁定——`deployment_canary_evidence_scope(target)` 對這五個欄位算出
  canonical SHA-256，`canary_target.evidence_directory` 必須**精確等於**這個
  digest 才會被接受（不再是 receipt 自由選的字串），因此實際 evidence 檔案
  必須真的存在於 `evidence_root/<digest>/...` 這個路徑之下——`evidence_root`
  是呼叫端信任邊界，receipt 內容無法寫入。同一份 wheel canary evidence 若被
  重貼到不同 profile_key／config_revision／instance／service／
  source_revision 的需求，其唯一合法路徑會跟著改變；除非攻擊者真的取得
  `evidence_root` 寫入權限並把檔案複製到新路徑，否則無法讓舊 evidence 通過
  新 target 的檢查——這已經超出單純竄改 receipt JSON 的威脅模型，是本模組
  唯一不受 receipt 內容操控的信任邊界。`cortex/task-memory-live-canary/v1`
  這個 kind 的 evidence 完全沒有檔案樹可供內容定址，目前仍只靠
  `evidence["target"] == 外層 target` 的自我一致檢查（見 `_verify_live` 已
  先驗證外層 target 就是這條需求 mapping 的 acceptance target）；這是已知
  的較弱綁定，非本輪對抗審查具體指出的項目，於此明確記載，不佇裝已解決。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import Any, Callable, Mapping

KIND_DEPLOYMENT_CANARY_QUALIFICATION = "cortex/deployment-canary-qualification/v1"
KIND_TASK_MEMORY_LIVE_CANARY = "cortex/task-memory-live-canary/v1"

# #845 對抗審查第九輪 MAJOR：deployment-canary qualification evidence 的 schema
# 完全沒有對應內容可以證明這五個 acceptance target 欄位；改用內容定址的
# evidence 目錄 locator 間接綁定（見 `deployment_canary_evidence_scope`）。
_DEPLOYMENT_CANARY_DIRECTORY_SCOPE_FIELDS = (
    "source_revision", "service", "instance", "profile_key", "config_revision",
)
_IMAGE_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

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


def _load_qualification_module(source_root: Path) -> ModuleType | None:
    """從 CLI `--source-root`（checkout 根目錄）動態載入 `qualification/validate.py`。

    wheel 不會打包 repo 根目錄的 `qualification/`；已安裝的 `cortex delivery` 在
    checkout 外執行時，一般 `import qualification` 只會撞運氣命中不相干或不存在
    的模組。這裡改成明確從呼叫端提供、已知是 checkout 根目錄的 `source_root`
    載入，路徑必須真的落在 `source_root` 之下且不得經過 symlink；缺檔、路徑逃逸
    或載入時任何例外都回傳 ``None``（fail-closed），由呼叫端在驗證該 kind 時
    直接拒絕，不拖累其他 kind。"""
    try:
        root = Path(source_root).resolve(strict=True)
        if not root.is_dir():
            return None
        package_dir = root / "qualification"
        module_path = package_dir / "validate.py"
        if package_dir.is_symlink() or module_path.is_symlink() or not module_path.is_file():
            return None
        resolved_module = module_path.resolve(strict=True)
        if root not in resolved_module.parents:
            return None
        # `validate.py` 以 `from qualification.contract import ...`（套件路徑）或
        # `from contract import ...`（host 端直接執行）取得 canary 契約。這裡同樣
        # 從同一個 `source_root/qualification/contract.py` 明確載入（相同的
        # symlink／路徑逃逸檢查），只在執行 validate 模組的當下暫時掛到這兩個
        # 名稱，結束後還原；不讓環境中其他 `qualification` 套件或 cwd 上的
        # `contract` 混進受治理驗證。source_root 沒有 contract.py 時，兩個名稱
        # 在執行期間設為 `None` 阻擋匯入：需要 contract 的 validate.py 因此
        # fail closed，不需要它的舊版 validate.py 不受影響。
        contract_module = None
        contract_path = package_dir / "contract.py"
        if contract_path.is_symlink():
            return None
        if contract_path.is_file():
            resolved_contract = contract_path.resolve(strict=True)
            if root not in resolved_contract.parents:
                return None
            contract_spec = importlib.util.spec_from_file_location(
                "paulsha_cortex._governed_qualification_contract", resolved_contract
            )
            if contract_spec is None or contract_spec.loader is None:
                return None
            contract_module = importlib.util.module_from_spec(contract_spec)
            contract_spec.loader.exec_module(contract_module)
        spec = importlib.util.spec_from_file_location(
            "paulsha_cortex._governed_qualification_validate", resolved_module
        )
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        aliases = ("qualification.contract", "contract")
        _missing = object()
        saved = {name: sys.modules.get(name, _missing) for name in aliases}
        try:
            for name in aliases:
                sys.modules[name] = contract_module  # None＝阻擋匯入
            spec.loader.exec_module(module)
        finally:
            for name, previous in saved.items():
                if previous is _missing:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = previous
        if not hasattr(module, "validate") or not hasattr(module, "ValidationError"):
            return None
        return module
    except (OSError, ValueError, ImportError, AttributeError, SyntaxError, TypeError):
        return None


def _resolve_evidence_directory(evidence_root: Path, locator: object) -> Path | None:
    """把 receipt 宣告的 evidence 目錄 locator 安全解析到 delivery `evidence_root`
    之下；`evidence_root` 是呼叫端（工廠建立時）提供的信任路徑，不受 receipt 內容
    影響。絕對路徑、`..`、反斜線或任何路徑元件是 symlink，都視為不安全並拒絕。"""
    if not isinstance(locator, str) or not locator:
        return None
    try:
        pure = PurePosixPath(locator)
    except TypeError:
        return None
    if (
        pure.is_absolute()
        or ".." in pure.parts
        or not pure.parts
        or "\\" in locator
        or "\x00" in locator
        or any(part in {"", "."} for part in pure.parts)
    ):
        return None
    root = Path(evidence_root)
    if root.is_symlink() or not root.is_dir():
        return None
    candidate = root
    for part in pure.parts:
        candidate = candidate / part
        if candidate.is_symlink():
            return None
    if not candidate.is_dir():
        return None
    return candidate


def deployment_canary_evidence_scope(target: Mapping[str, Any]) -> str:
    """deployment-canary evidence 目錄的規範內容定址 locator（#845 對抗審查
    第九輪 MAJOR）。

    只吃 `target` 裡 qualification evidence 無法直接證明的五個欄位
    （`_DEPLOYMENT_CANARY_DIRECTORY_SCOPE_FIELDS`），算出 canonical JSON 的
    SHA-256；`_validate_deployment_canary_qualification` 要求
    `canary_target.evidence_directory` 精確等於這個值，而不是 receipt 自由
    選擇的字串。這是純函式，不讀檔案系統；實際 evidence 是否真的存在於這個
    digest 命名的目錄下，由 `_resolve_evidence_directory` 對呼叫端信任邊界
    `evidence_root` 解析後核對。"""
    payload = {field: target.get(field) for field in _DEPLOYMENT_CANARY_DIRECTORY_SCOPE_FIELDS}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def derive_canary_domain(receipt: Mapping[str, Any]) -> str | None:
    """依 `receipt["kind"]` 從已通過 schema 檢查的 evidence 導出 canary 執行
    環境身分，取代 receipt 自報的 `independence.canary_domain`（#845 對抗
    審查第九輪 BLOCKER）。

    - `cortex/deployment-canary-qualification/v1`：導出來源是 evidence 的
      `image.digest`（`sha256:` + 64 hex；`qualification/driver.py` docstring
      明示這是複製進候選掛載之前的參考映像，與候選 checkout／reviewer 執行
      環境無關）。
    - `cortex/task-memory-live-canary/v1`：導出來源是 evidence 的
      `executor.id`（非空字串；本模組新增的必要欄位，代表產生這份 canary
      量測的執行者身分）。

    kind 未登記、evidence 型別不符或對應欄位缺失／格式錯誤，一律回傳
    ``None``（fail-closed）；呼叫端（`requirement_delivery._verify_live`）
    不得因為看不懂就代入預設值放行。"""
    if not isinstance(receipt, Mapping):
        return None
    kind = receipt.get("kind")
    evidence = receipt.get("evidence")
    if not isinstance(kind, str) or not isinstance(evidence, Mapping):
        return None
    if kind == KIND_DEPLOYMENT_CANARY_QUALIFICATION:
        image = evidence.get("image")
        digest = image.get("digest") if isinstance(image, Mapping) else None
        if not isinstance(digest, str) or _IMAGE_DIGEST_RE.fullmatch(digest) is None:
            return None
        return f"deployment-canary-image:{digest}"
    if kind == KIND_TASK_MEMORY_LIVE_CANARY:
        executor = evidence.get("executor")
        executor_id = executor.get("id") if isinstance(executor, Mapping) else None
        if not isinstance(executor_id, str) or not executor_id:
            return None
        return f"task-memory-live-canary-executor:{executor_id}"
    return None


def _validate_deployment_canary_qualification(
    receipt: Mapping[str, Any],
    *,
    qualification_module: ModuleType | None,
    evidence_root: Path,
) -> bool:
    """deployment-canary `qualification.json` receipt：沿用 `qualification/validate.py`
    既有驗證邏輯，不另寫一套；以 `require_canary_profile=True` 驗證，額外綁定
    candidate_sha／wheel sha256 等於 target，以及外部 canary 身分
    （repository／work_id／issue）與真正落地的 evidence 目錄。任一項缺失即拒絕。"""
    if qualification_module is None:
        # `qualification/validate.py` 未能從 --source-root 載入；fail closed，
        # 不得因為缺套件或路徑不安全就放行（#845 對抗審查 MAJOR）。
        return False
    target = receipt.get("target")
    evidence = receipt.get("evidence")
    canary_target = receipt.get("canary_target")
    if not isinstance(target, Mapping) or not isinstance(evidence, Mapping) or not isinstance(canary_target, Mapping):
        return False
    candidate_sha = target.get("candidate_sha")
    wheel_sha256 = target.get("artifact_sha256")
    if not isinstance(candidate_sha, str) or not candidate_sha or not isinstance(wheel_sha256, str) or not wheel_sha256:
        return False
    if evidence.get("profile") != "deployment-canary":
        return False

    # 外部 canary 身分：不得只信 evidence 自己在 dispatch-closeout 裡宣稱的
    # repository／work_id／issue，必須另外由 receipt 帶入且 repository 綁定
    # 已驗證的 target["repo"]，交給 `validate()` 逐一核對 evidence 內容是否一致。
    canary_repository = canary_target.get("repository")
    canary_work_id = canary_target.get("work_id")
    canary_issue = canary_target.get("issue")
    evidence_directory = canary_target.get("evidence_directory")
    if (
        not isinstance(canary_repository, str)
        or not canary_repository
        or canary_repository != target.get("repo")
        or not isinstance(canary_work_id, str)
        or not canary_work_id
        or isinstance(canary_issue, bool)
        or not isinstance(canary_issue, int)
        or canary_issue <= 0
    ):
        return False

    # #845 對抗審查第九輪 MAJOR：qualification evidence 完全無法證明
    # source_revision／service／instance／profile_key／config_revision 這五個
    # target 欄位；改用內容定址間接綁定——evidence_directory 必須精確等於這
    # 五個欄位的規範 digest，不再是 receipt 自由選擇的字串。同一份 wheel
    # canary evidence 換掉這五個欄位後，其唯一合法路徑會跟著改變，無法只靠
    # 竄改 receipt JSON 就讓舊 evidence 通過新 target。
    if evidence_directory != deployment_canary_evidence_scope(target):
        return False

    resolved_evidence_root = _resolve_evidence_directory(evidence_root, evidence_directory)
    if resolved_evidence_root is None:
        return False

    try:
        qualification_module.validate(
            evidence,
            candidate_sha=candidate_sha,
            wheel_sha256=wheel_sha256,
            bundle_sha256=None,
            evidence_root=resolved_evidence_root,
            require_release_profile=False,
            require_canary_profile=True,
            canary_repository=canary_repository,
            canary_work_id=canary_work_id,
            canary_issue=canary_issue,
        )
    except qualification_module.ValidationError:
        return False
    except (TypeError, ValueError, KeyError, OSError):
        return False
    return True


def _validate_task_memory_live_canary(
    receipt: Mapping[str, Any],
    *,
    qualification_module: ModuleType | None = None,
    evidence_root: Path | None = None,
) -> bool:
    """#857 task-memory canary evidence：content retrieval／各 path 成功率達門檻，
    負例與跨 project 檢查通過，且綁定同一個 target（即同時期 #841 loaded runtime
    receipt 的 artifact digest／revision）；無法綁定即拒絕。"""
    target = receipt.get("target")
    evidence = receipt.get("evidence")
    if not isinstance(target, Mapping) or not isinstance(evidence, Mapping):
        return False
    if evidence.get("schema") != KIND_TASK_MEMORY_LIVE_CANARY or evidence.get("passed") is not True:
        return False
    # #845 對抗審查第九輪 BLOCKER：`derive_canary_domain` 需要一個獨立於
    # review 執行環境的執行者身分才能導出 canary_domain；這個 kind 原本沒有
    # 任何身分欄位可用，因此新增為必要欄位（非空字串），不是選填。
    executor = evidence.get("executor")
    executor_id = executor.get("id") if isinstance(executor, Mapping) else None
    if not isinstance(executor_id, str) or not executor_id:
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


_VALIDATORS: dict[str, Callable[..., bool]] = {
    KIND_DEPLOYMENT_CANARY_QUALIFICATION: _validate_deployment_canary_qualification,
    KIND_TASK_MEMORY_LIVE_CANARY: _validate_task_memory_live_canary,
}


def make_governed_live_receipt_validator(
    *, source_root: str | Path, evidence_root: str | Path
) -> Callable[[Mapping[str, Any]], bool]:
    """建立 `requirement_delivery` 唯一的 production `live_receipt_validator`。

    以 closure 帶入呼叫端（`porcelain/delivery.py`）已經驗證過的兩個路徑：

    - ``source_root``：CLI `--source-root`，checkout 根目錄，用來動態載入
      `qualification/validate.py`（#845 對抗審查 MAJOR：不再依賴一般 import
      撞運氣命中 repo 根目錄）。
    - ``evidence_root``：delivery evidence root（目前是 Manager coordinator
      root），deployment-canary receipt 綁定的 evidence 目錄只能解析到這個
      根目錄之下（#845 對抗審查 BLOCKER：不再略過 evidence tree 的逐檔案核對）。

    回傳的 callable 仍然只吃 receipt 本身，符合 `_verify_live` 既有呼叫慣例——
    這兩個路徑是呼叫端事先决定、不受 receipt 內容操控的信任邊界，不是從 receipt
    推導出來的。封閉登記表：只接受本模組明確登記且可機械驗證的 receipt kind；
    未知 kind、型別不符或任何解析例外都回傳 ``False``（fail-closed），不得因為
    看不懂或內部壞掉而放行。此函式讀取傳入的 ``receipt`` 內容，不觸發模型、
    merge、部署或關票，亦不讀取檔案系統或網路（deployment-canary kind 底下
    對 evidence_root 的檔案讀取，全部委由已載入的 `qualification/validate.py`
    在其自身 fail-closed 契約內完成）。
    """
    resolved_evidence_root = Path(evidence_root)
    qualification_module = _load_qualification_module(Path(source_root))

    def validator(receipt: Mapping[str, Any]) -> bool:
        if not isinstance(receipt, Mapping):
            return False
        kind = receipt.get("kind")
        if not isinstance(kind, str):
            return False
        handler = _VALIDATORS.get(kind)
        if handler is None:
            return False
        try:
            return handler(
                receipt,
                qualification_module=qualification_module,
                evidence_root=resolved_evidence_root,
            ) is True
        except Exception:
            return False

    return validator
