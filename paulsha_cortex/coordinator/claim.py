"""Pure claim policy used by the Manager single-writer workflow."""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from pathlib import PurePosixPath
from urllib.parse import quote

from paulsha_cortex.config import paths
from paulsha_cortex.deck.schema import BAND_LEVELS
from paulsha_cortex.github_rate_limit import is_rate_limit_signal

from . import verification

AUTO_LABEL = "cortex:auto-on-going"
WORK_SNAPSHOT_SCHEMA = "work-items-snapshot/v1"
GITHUB_PROVIDER_ID = "github"
PROVIDER_MAX_AGE_SECONDS = 900
DERIVED_AUTHORITY_KINDS = frozenset({"workflow_run", "completion_record"})

# #206：穩定 reason code，供 upstream（durable done record／manager log）在不
# 重新解析訊息文字的情況下辨識是哪一種 authority 驗證失敗。canonical 與 legacy
# schema 各自的 provider 失敗一律區分 -canonical / -legacy 後綴（AC3）。
REASON_ROW_MALFORMED = "row-malformed"
REASON_IDENTITY_INVALID = "identity-invalid"
REASON_PROVIDER_MISSING_CANONICAL = "provider-authority-missing-canonical"
REASON_PROVIDER_INVALID_CANONICAL = "provider-authority-invalid-canonical"
REASON_PROVIDER_MISSING_LEGACY = "provider-authority-missing-legacy"
REASON_PROVIDER_INVALID_LEGACY = "provider-authority-invalid-legacy"
# #370：canonical provider 因 rate limit degraded 是暫時性的，與其他
# authority-invalid 情境（missing/malformed/停擺）分開分類，讓 resume 的
# durable backoff 能認得出「等待 reset 即可」而非「需要人工排查」。
REASON_PROVIDER_RATE_LIMITED_CANONICAL = "provider-authority-rate-limited-canonical"
# #389：canonical row 本身存在、可解析，但因 lifecycle 尚未給出可 claim 的形狀
# 而被 `_authority_from_canonical_row` 略過的三種情境。修法前這三種分支一律
# `return None`：呼叫端（`_load_work_authorities_with_diagnostics`）只把它們
# 當成「這列不算」悄悄丟棄、不留診斷，於是 `load_work_authority` 找不到目標
# 也找不到 skip 診斷，只能落回與「row 根本不存在」「issue 被多個 work_id 認領」
# 共用的泛化 `confirmed work authority missing or ambiguous`。三者各自獨立
# reason code，讓 load_work_authority 能對「目標就是這一種」給專屬訊息。
REASON_AUTHORITY_ALL_INFERRED = "authority-all-inferred"
REASON_AUTHORITY_NOT_STARTABLE = "authority-not-startable"
REASON_AUTHORITY_NO_TODO_SOURCE = "authority-no-confirmed-todo-source"

_UNSAFE_LABEL_PREFIXES = ("/", "~")


def _diagnostic_label(value: object, *, max_len: int = 200) -> str | None:
    """Best-effort, secret-free label for a snapshot row's identity field.

    Only ever attached to :class:`AuthorityValidationError` for diagnostics
    — never used for authorization decisions. Rejects values that look like
    filesystem paths so a malformed row can never smuggle an absolute path
    into a durable error message (tier: shareable — AI-SEC-001 契約）.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text.startswith(_UNSAFE_LABEL_PREFIXES) or "\\" in text:
        return None
    if len(text) > max_len:
        text = text[:max_len] + "…"
    return text


class AuthorityValidationError(ValueError):
    """Row-scoped authority validation failure with secret-free diagnostics.

    Carries a stable ``reason_code`` plus ``repo``/``work_id``/
    ``provider_id``/``field`` (#206 AC1/AC3) so upstream durable done
    records and Manager logs can record *which* mutation failed without
    re-parsing message text. Remains a ``ValueError`` subclass so existing
    ``except ValueError`` call sites keep working unchanged.
    """

    def __init__(
        self,
        message: str,
        *,
        reason_code: str,
        repo: str | None = None,
        work_id: str | None = None,
        provider_id: str | None = None,
        field: str | None = None,
    ) -> None:
        self.reason_code = reason_code
        self.repo = repo
        self.work_id = work_id
        self.provider_id = provider_id
        self.field = field
        self.base_message = message
        details = [f"reason={reason_code}"]
        for label, value in (
            ("repo", repo),
            ("work_id", work_id),
            ("provider_id", provider_id),
            ("field", field),
        ):
            if value is not None:
                details.append(f"{label}={value}")
        super().__init__(f"{message} ({', '.join(details)})")


def semantic_source_revision(
    *,
    repo: str,
    kind: str,
    ref: str,
    source_id: str,
    revision: str,
    status: str | None = None,
) -> tuple[str, str] | None:
    """Return the stable security authority represented by one Monitor source.

    Workflow/completion rows are projections of Manager state and must never
    feed back into a new claim. GitHub timestamps and active/archive OpenSpec
    provider locators are provenance; their closure facts are checked by
    dedicated gates, so identity—not updated_at—is the stable authority here.
    Source membership and locator identity are the claim authority. Provider
    timestamps and content hashes remain provenance: changing either must not
    make a Manager-authored archive/PR refresh look like a second claim. A
    changed target is still security relevant because it changes the stable
    source key/ref set and therefore the authority digest.
    """

    if kind in DERIVED_AUTHORITY_KINDS:
        return None
    if kind in {"github_issue", "github_pr"}:
        state = str(status or "").lower()
        allowed = (
            {"open", "closed"}
            if kind == "github_issue"
            else {"open", "closed", "merged"}
        )
        if state not in allowed:
            raise AuthorityValidationError(
                f"canonical {kind} lifecycle status invalid",
                reason_code=REASON_ROW_MALFORMED,
                repo=_diagnostic_label(repo),
                field="status",
            )
        return source_id, f"identity:{ref};state:{state}"
    if kind in {"todo", "superpowers_spec", "superpowers_plan"}:
        return source_id, f"identity:{ref}"
    if kind == "openspec":
        state = str(status or "").lower()
        if state not in {"active", "archived"}:
            raise AuthorityValidationError(
                "canonical openspec lifecycle status invalid",
                reason_code=REASON_ROW_MALFORMED,
                repo=_diagnostic_label(repo),
                field="status",
            )
        return f"openspec:{repo}:{ref}", f"identity:{ref};state:{state}"
    return source_id, revision


@dataclass(frozen=True, init=False)
class WorkAuthority:
    repo: str
    work_id: str
    mapped_issues: tuple[int, ...]
    mapped_prs: tuple[int, ...]
    mapped_openspec: tuple[str, ...]
    mapped_todo_paths: tuple[str, ...]
    confirmed_todo: bool
    auto_label: bool
    source_revisions: tuple[str, ...]
    github_provider_id: str
    github_provider_revision: str
    github_last_success_epoch: float
    snapshot_hash: str
    #: `#530`：本 work item 的 confirmed sources 是否真的由 GitHub provider 供應。
    #: 為 False 時（權威全部來自本機檔案系統 provider，例如只掛一份 workstream
    #: `todo.md` 的 work item），GitHub provider 的健康度與新鮮度與這筆工作無關，
    #: 不得用來擋 claim——否則一次 GitHub 可用性事故會放大成整個 fleet 的派工停擺。
    #: 預設 True（保守：legacy schema 與未宣告來源者一律沿用嚴格 fail-closed）。
    requires_github_authority: bool = True

    @classmethod
    def _verified(
        cls,
        *,
        repo: str,
        work_id: str,
        mapped_issues: tuple[int, ...],
        mapped_prs: tuple[int, ...] = (),
        mapped_openspec: tuple[str, ...] = (),
        mapped_todo_paths: tuple[str, ...] = (),
        confirmed_todo: bool,
        auto_label: bool,
        source_revisions: tuple[str, ...],
        provider_revision: str,
        provider_id: str = GITHUB_PROVIDER_ID,
        last_success_epoch: float,
        snapshot_hash: str,
        requires_github_authority: bool = True,
    ) -> "WorkAuthority":
        authority = object.__new__(cls)
        object.__setattr__(authority, "repo", repo)
        object.__setattr__(authority, "work_id", work_id)
        object.__setattr__(authority, "mapped_issues", mapped_issues)
        object.__setattr__(authority, "mapped_prs", mapped_prs)
        object.__setattr__(authority, "mapped_openspec", mapped_openspec)
        object.__setattr__(authority, "mapped_todo_paths", mapped_todo_paths)
        object.__setattr__(authority, "confirmed_todo", confirmed_todo)
        object.__setattr__(authority, "auto_label", auto_label)
        object.__setattr__(authority, "source_revisions", source_revisions)
        object.__setattr__(authority, "github_provider_id", provider_id)
        object.__setattr__(authority, "github_provider_revision", provider_revision)
        object.__setattr__(authority, "github_last_success_epoch", last_success_epoch)
        object.__setattr__(authority, "snapshot_hash", snapshot_hash)
        object.__setattr__(
            authority, "requires_github_authority", bool(requires_github_authority)
        )
        return authority


def canonical_work_snapshot_path() -> Path:
    root = os.environ.get("PSC_MONITOR_STATE_ROOT", "").strip()
    state_root = Path(root).expanduser() if root else paths.agents_root() / "monitor"
    return state_root / "work-items.snapshot.json"


def _load_snapshot(snapshot_path: str | Path | None = None) -> tuple[dict, str]:
    path = Path(snapshot_path) if snapshot_path is not None else canonical_work_snapshot_path()
    if path.is_symlink() or not path.is_file():
        raise ValueError("durable work snapshot unavailable")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("durable work snapshot unreadable") from exc
    if not isinstance(payload, dict) or payload.get("schema") != WORK_SNAPSHOT_SCHEMA:
        raise ValueError("durable work snapshot schema invalid")
    providers = payload.get("providers")
    items = payload.get("work_items")
    if not isinstance(providers, dict) or not isinstance(items, list):
        raise ValueError("durable work snapshot malformed")
    github = providers.get(GITHUB_PROVIDER_ID)
    if github is None:
        # PR A canonical schema keys GitHub providers by repo.
        return payload, verification.canonical_json_hash(payload)
    if not isinstance(github, dict) or github.get("provider_id") != GITHUB_PROVIDER_ID:
        raise AuthorityValidationError(
            "durable GitHub provider authority missing",
            reason_code=REASON_PROVIDER_MISSING_LEGACY,
            provider_id=GITHUB_PROVIDER_ID,
            field="provider_id",
        )
    revision = github.get("revision")
    last_success = github.get("last_success_epoch")
    degraded = github.get("degraded")
    if (
        not isinstance(revision, str)
        or not revision.strip()
        or not isinstance(last_success, (int, float))
        or isinstance(last_success, bool)
        or not math.isfinite(float(last_success))
        or not isinstance(degraded, bool)
        or degraded
    ):
        if not isinstance(revision, str) or not revision.strip():
            field = "revision"
        elif (
            not isinstance(last_success, (int, float))
            or isinstance(last_success, bool)
            or not math.isfinite(float(last_success))
        ):
            field = "last_success_epoch"
        else:
            field = "degraded"
        raise AuthorityValidationError(
            "durable GitHub provider authority invalid",
            reason_code=REASON_PROVIDER_INVALID_LEGACY,
            provider_id=GITHUB_PROVIDER_ID,
            field=field,
        )
    return payload, verification.canonical_json_hash(payload)


def mapped_issue_titles(
    authority: WorkAuthority,
    *,
    snapshot_path: str | Path | None = None,
) -> dict[int, str | None] | None:
    try:
        payload, canonical_hash = _load_snapshot(snapshot_path)
    except ValueError:
        # 呼叫端（work_bridge.start_canonical_workflow → select_combo）已把
        # None 當成「拿不到權威 issue 標題」的既定 bypass 訊號（見下方 hash
        # mismatch 分支）。_load_snapshot 在 durable snapshot 不存在／不可
        # 讀／schema 損壞，或（legacy schema）provider 區塊本身無效時皆會
        # raise ValueError（AuthorityValidationError 亦是其子類別）——這些
        # 都是「這次就是拿不到權威資料」的同一類情境，理應與 hash mismatch
        # 走同一條 fail-soft 路徑，而不是讓例外一路炸穿到 claim 呼叫端。
        # 其餘呼叫者（load_work_authorities／load_work_authority）需要的是
        # 一個可信的 WorkAuthority 本體，沒有安全的預設值可以退，所以維持
        # 現行的 fail-hard 行為不變，只有這裡改。
        return None
    if canonical_hash != authority.snapshot_hash:
        return None
    for row in payload.get("work_items", []):
        if (
            isinstance(row, dict)
            and row.get("repo") == authority.repo
            and row.get("work_id") == authority.work_id
        ):
            sources = row.get("sources")
            if not isinstance(sources, list):
                return {}
            titles: dict[int, str | None] = {}
            for source in sources:
                if not isinstance(source, dict) or source.get("kind") != "github_issue":
                    continue
                match = re.fullmatch(
                    rf"{re.escape(authority.repo)}#([1-9][0-9]*)",
                    str(source.get("ref") or ""),
                )
                if match is None:
                    continue
                number = int(match.group(1))
                title = source.get("title")
                titles[number] = title if isinstance(title, str) else None
            return titles
    return {}


def _authority_from_row(
    *,
    row: object,
    providers: dict,
    snapshot_hash: str,
    allow_rate_limited_last_known_good: bool = False,
) -> WorkAuthority | None:
    if not isinstance(row, dict):
        raise AuthorityValidationError(
            "confirmed work authority row malformed",
            reason_code=REASON_ROW_MALFORMED,
        )
    repo = row.get("repo")
    work_id = row.get("work_id")
    repo_label = _diagnostic_label(repo)
    work_id_label = _diagnostic_label(work_id)
    if "mapped_issues" not in row:
        return _authority_from_canonical_row(
            row=row,
            providers=providers,
            snapshot_hash=snapshot_hash,
            allow_rate_limited_last_known_good=allow_rate_limited_last_known_good,
        )
    github = providers.get(GITHUB_PROVIDER_ID)
    if not isinstance(github, dict):
        raise AuthorityValidationError(
            "durable GitHub provider authority missing",
            reason_code=REASON_PROVIDER_MISSING_LEGACY,
            repo=repo_label,
            work_id=work_id_label,
            provider_id=GITHUB_PROVIDER_ID,
            field="providers.github",
        )
    issues = row.get("mapped_issues")
    prs = row.get("mapped_prs", [])
    changes = row.get("mapped_openspec", [])
    todo_paths = row.get("mapped_todo_paths", [])
    confirmed_todo = row.get("confirmed_todo")
    auto_label = row.get("auto_label", False)
    source_revisions = row.get("source_revisions")
    if (
        not isinstance(issues, list)
        or any(not isinstance(issue, int) or isinstance(issue, bool) or issue <= 0 for issue in issues)
        or len(set(issues)) != len(issues)
        or not isinstance(prs, list)
        or any(not isinstance(pr, int) or isinstance(pr, bool) or pr <= 0 for pr in prs)
        or len(set(prs)) != len(prs)
        or not isinstance(changes, list)
        or any(
            not isinstance(change, str)
            or re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", change) is None
            for change in changes
        )
        or len(set(changes)) != len(changes)
        or not isinstance(todo_paths, list)
        or any(not _safe_todo_path(path) for path in todo_paths)
        or len(set(todo_paths)) != len(todo_paths)
    ):
        raise AuthorityValidationError(
            "confirmed work authority mapped issues invalid",
            reason_code=REASON_ROW_MALFORMED,
            repo=repo_label,
            work_id=work_id_label,
            field="mapped_issues",
        )
    repo_valid = isinstance(repo, str) and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) is not None
    work_id_valid = isinstance(work_id, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]*", work_id) is not None
    if not repo_valid or not work_id_valid:
        raise AuthorityValidationError(
            "confirmed work authority identity invalid",
            reason_code=REASON_IDENTITY_INVALID,
            repo=repo_label,
            work_id=work_id_label,
            field="repo" if not repo_valid else "work_id",
        )
    if not isinstance(confirmed_todo, bool):
        raise AuthorityValidationError(
            "confirmed work authority Todo flag invalid",
            reason_code=REASON_ROW_MALFORMED,
            repo=repo_label,
            work_id=work_id_label,
            field="confirmed_todo",
        )
    if not isinstance(auto_label, bool):
        raise AuthorityValidationError(
            "confirmed work authority auto label invalid",
            reason_code=REASON_ROW_MALFORMED,
            repo=repo_label,
            work_id=work_id_label,
            field="auto_label",
        )
    if (
        not isinstance(source_revisions, list)
        or not source_revisions
        or any(not isinstance(value, str) or not value.strip() for value in source_revisions)
    ):
        raise AuthorityValidationError(
            "confirmed work authority revisions invalid",
            reason_code=REASON_ROW_MALFORMED,
            repo=repo_label,
            work_id=work_id_label,
            field="source_revisions",
        )
    return WorkAuthority._verified(
        repo=repo,
        work_id=work_id,
        mapped_issues=tuple(sorted(issues)),
        mapped_prs=tuple(sorted(prs)),
        mapped_openspec=tuple(sorted(changes)),
        mapped_todo_paths=tuple(sorted(todo_paths)),
        confirmed_todo=confirmed_todo,
        auto_label=auto_label,
        source_revisions=tuple(sorted(source_revisions)),
        provider_revision=github["revision"].strip(),
        last_success_epoch=float(github["last_success_epoch"]),
        snapshot_hash=snapshot_hash,
    )


def _auto_label_from_observations(github: dict, issues: list[int]) -> bool:
    """由 github provider 的 observations 判定 work item 是否掛 auto 派工 label。

    來源：`monitor/providers.py` 的 `GitHubWorkProvider` 把持有 AUTO_LABEL 的
    open issue 編號寫進 `observations["auto_label_issues"]`（issues 回應本來就含
    labels，鏡像判定零額外 API）。缺失／形狀不合 → 保守 `False`。
    """

    observations = github.get("observations")
    if not isinstance(observations, dict):
        return False
    raw = observations.get("auto_label_issues")
    if not isinstance(raw, list):
        return False
    labeled: set[int] = set()
    for value in raw:
        if isinstance(value, bool) or not isinstance(value, int):
            return False
        labeled.add(value)
    return any(number in labeled for number in issues)


def _authority_from_canonical_row(
    *,
    row: dict,
    providers: dict,
    snapshot_hash: str,
    allow_rate_limited_last_known_good: bool = False,
) -> WorkAuthority | None:
    repo = row.get("repo")
    work_id = row.get("work_id")
    sources = row.get("sources")
    repo_label = _diagnostic_label(repo)
    work_id_label = _diagnostic_label(work_id)
    if not isinstance(repo, str) or not isinstance(work_id, str) or not isinstance(sources, list):
        raise AuthorityValidationError(
            "canonical work authority row malformed",
            reason_code=REASON_ROW_MALFORMED,
            repo=repo_label,
            work_id=work_id_label,
        )
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) is None:
        raise AuthorityValidationError(
            "canonical work authority identity invalid",
            reason_code=REASON_IDENTITY_INVALID,
            repo=repo_label,
            work_id=work_id_label,
            field="repo",
        )
    next_actions = row.get("next_actions")
    if next_actions is not None and (
        not isinstance(next_actions, list)
        or any(not isinstance(action, str) or not action for action in next_actions)
    ):
        raise AuthorityValidationError(
            "canonical work authority actions malformed",
            reason_code=REASON_ROW_MALFORMED,
            repo=repo_label,
            work_id=work_id_label,
            field="next_actions",
        )
    if any(
        not isinstance(source, dict)
        or source.get("confidence") not in {"confirmed", "inferred"}
        for source in sources
    ):
        raise AuthorityValidationError(
            "canonical work authority sources malformed",
            reason_code=REASON_ROW_MALFORMED,
            repo=repo_label,
            work_id=work_id_label,
            field="sources",
        )
    # #389 診斷訊息一律用 `work_id_label`（已過 `_diagnostic_label` 安全過濾），
    # 不得直接把 `work_id` 原始值嵌進訊息文字——此時 `work_id` 只確定是
    # `str`，尚未通過下方的 identity 安全正規式驗證，直接嵌入會繞過
    # `_diagnostic_label` 既有的「拒絕看似檔案路徑的值」防線（tier: shareable
    # — AI-SEC-001 契約，見檔案頂部 `_diagnostic_label` docstring）。
    safe_work_id = work_id_label or "<redacted>"
    if sources and all(source["confidence"] == "inferred" for source in sources):
        raise AuthorityValidationError(
            f"work item '{safe_work_id}' has no confirmed sources yet (all "
            "sources are inferred): cannot be evaluated for claiming until "
            "at least one source is confirmed",
            reason_code=REASON_AUTHORITY_ALL_INFERRED,
            repo=repo_label,
            work_id=work_id_label,
            field="sources",
        )
    confirmed = [
        source
        for source in sources
        if isinstance(source, dict) and source.get("confidence") == "confirmed"
    ]
    has_workflow = any(source.get("kind") == "workflow_run" for source in confirmed)
    if next_actions is not None and "start" not in next_actions and not has_workflow:
        state_label = _diagnostic_label(row.get("state")) or "not-startable"
        raise AuthorityValidationError(
            f"work item '{safe_work_id}' is in '{state_label}' state: needs "
            "an active todo source (workstream todo.md path link or openspec "
            "change) to become claimable",
            reason_code=REASON_AUTHORITY_NOT_STARTABLE,
            repo=repo_label,
            work_id=work_id_label,
            field="next_actions",
        )
    todo_kinds = {"todo", "superpowers_spec", "superpowers_plan", "openspec"}
    if not any(source.get("kind") in todo_kinds for source in confirmed):
        raise AuthorityValidationError(
            f"work item '{safe_work_id}' has no confirmed todo-kind source "
            "(todo/superpowers_spec/superpowers_plan/openspec): needs an "
            "active todo source (workstream todo.md path link or openspec "
            "change) to become claimable",
            reason_code=REASON_AUTHORITY_NO_TODO_SOURCE,
            repo=repo_label,
            work_id=work_id_label,
            field="sources",
        )
    if re.fullmatch(r"[a-z0-9][a-z0-9-]*", work_id) is None:
        raise AuthorityValidationError(
            "canonical work authority identity invalid",
            reason_code=REASON_IDENTITY_INVALID,
            repo=repo_label,
            work_id=work_id_label,
            field="work_id",
        )
    # #530：這筆 work item 的 confirmed sources 究竟由誰供應？只有真的用到 GitHub
    # 的 work item 才該被 GitHub provider 的健康度擋下。
    #
    # 判準以 provider id 前綴為主（`github:`／`github-terminal:` 都是 GitHub 來源，
    # `repo:` 是本機檔案系統），kind 為第二道保險——remote todo／openspec 由
    # `github-terminal:` 供應，只看 kind 會漏掉它們。
    #
    # **資訊缺席一律保守**：`provider` 欄位缺失或非字串時視為「可能來自 GitHub」，
    # 維持既有的嚴格 fail-closed。放寬只發生在 source 明確標示了非 GitHub provider
    # 的情況——亦即我們有正面證據證明這筆工作不依賴 GitHub，而不是「沒看到證據」。
    def _source_needs_github(source: dict) -> bool:
        if source.get("kind") in {"github_issue", "github_pr"}:
            return True
        provider = source.get("provider")
        if not isinstance(provider, str) or not provider:
            return True
        return provider.startswith(GITHUB_PROVIDER_ID)

    requires_github_authority = any(_source_needs_github(source) for source in confirmed)
    provider_id = f"github:{repo}"
    github = providers.get(provider_id)
    if not isinstance(github, dict):
        raise AuthorityValidationError(
            "durable GitHub provider authority missing",
            reason_code=REASON_PROVIDER_MISSING_CANONICAL,
            repo=repo_label,
            work_id=work_id_label,
            provider_id=provider_id,
            field="providers",
        )
    revision = github.get("revision")
    last_success_at = github.get("last_success_at")
    status = github.get("status")
    if (
        status != "ok"
        or not isinstance(revision, str)
        or not revision
        or not isinstance(last_success_at, str)
    ):
        # #370：provider 因 rate limit 而 degraded 是暫時性的（reset 後自然
        # 恢復），不是真正的 authority 損毀——upstream（resume 的 durable
        # backoff、Manager log）需要能不重新解析訊息文字就分辨出這種情況，
        # 才能給出「限流中、稍後自動重試」而非要求人工介入。訊號來源是
        # Monitor GitHubWorkProvider.scan() 寫入的 diagnostics（見
        # monitor/providers.py），本身已用同一套 github_rate_limit 分類器產生。
        rate_limited = False
        if status != "ok":
            diagnostics = github.get("diagnostics")
            rate_limited = isinstance(diagnostics, list) and any(
                isinstance(entry, str) and is_rate_limit_signal(entry) for entry in diagnostics
            )
        # #370 follow-up：退休語境（work_actions._RETIREMENT_ACTIONS）不依賴
        # issue 即時開關狀態，只要 snapshot 還留有先前成功的 last-known-good
        # revision/last_success_at 就能安全續行——正好在系統被限流、最需要清
        # stuck run 的時候放行清理。僅在「rate-limit degraded 且 last-known-good
        # 齊全」的窄條件下豁免；claim/start 等需要即時 authority 的語境維持預設
        # False，一律 fail-closed。其餘 degraded／缺 revision／壞 timestamp 仍嚴
        # 格拒絕。
        have_last_known_good = (
            isinstance(revision, str) and bool(revision) and isinstance(last_success_at, str)
        )
        # #530：不依賴 GitHub 的 work item（confirmed sources 全部來自本機檔案系統
        # provider）只要 snapshot 還留有 last-known-good 的 revision/timestamp，就
        # 沿用它建構 authority——GitHub 此刻健不健康與這筆工作無關。這道豁免與上面
        # 的退休語境豁免（#370 follow-up）條件並列而非取代：兩者都要求
        # last-known-good 齊全，差別只在放行的理由。缺 revision／壞 timestamp 仍嚴格拒絕。
        github_authority_waived = not requires_github_authority and have_last_known_good
        if not (
            github_authority_waived
            or (allow_rate_limited_last_known_good and rate_limited and have_last_known_good)
        ):
            if status != "ok":
                if rate_limited:
                    raise AuthorityValidationError(
                        "durable GitHub provider authority rate-limited",
                        reason_code=REASON_PROVIDER_RATE_LIMITED_CANONICAL,
                        repo=repo_label,
                        work_id=work_id_label,
                        provider_id=provider_id,
                        field="status",
                    )
                field = "status"
            elif not isinstance(revision, str) or not revision:
                field = "revision"
            else:
                field = "last_success_at"
            raise AuthorityValidationError(
                "durable GitHub provider authority invalid",
                reason_code=REASON_PROVIDER_INVALID_CANONICAL,
                repo=repo_label,
                work_id=work_id_label,
                provider_id=provider_id,
                field=field,
            )
        # else：rate-limited 但被退休語境豁免——沿用 last-known-good 的
        # revision/last_success_at 繼續建構 authority。
    try:
        last_success = datetime.fromisoformat(last_success_at.replace("Z", "+00:00")).timestamp()
    except ValueError as exc:
        raise AuthorityValidationError(
            "durable GitHub provider timestamp invalid",
            reason_code=REASON_PROVIDER_INVALID_CANONICAL,
            repo=repo_label,
            work_id=work_id_label,
            provider_id=provider_id,
            field="last_success_at",
        ) from exc
    issues: list[int] = []
    prs: list[int] = []
    changes: list[str] = []
    todo_paths: list[str] = []
    for source in confirmed:
        kind = source.get("kind")
        ref = source.get("ref")
        if kind in {"github_issue", "github_pr"}:
            match = re.fullmatch(rf"{re.escape(repo)}#([1-9][0-9]*)", str(ref or ""))
            if match is None:
                raise AuthorityValidationError(
                    "canonical GitHub work source ref invalid",
                    reason_code=REASON_ROW_MALFORMED,
                    repo=repo_label,
                    work_id=work_id_label,
                    field="sources.ref",
                )
            target = issues if kind == "github_issue" else prs
            target.append(int(match.group(1)))
        elif kind == "openspec":
            if not isinstance(ref, str) or re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", ref) is None:
                raise AuthorityValidationError(
                    "canonical OpenSpec work source ref invalid",
                    reason_code=REASON_ROW_MALFORMED,
                    repo=repo_label,
                    work_id=work_id_label,
                    field="sources.ref",
                )
            changes.append(ref)
        elif kind == "todo":
            if not _safe_todo_path(ref):
                raise AuthorityValidationError(
                    "canonical Todo work source ref invalid",
                    reason_code=REASON_ROW_MALFORMED,
                    repo=repo_label,
                    work_id=work_id_label,
                    field="sources.ref",
                )
            todo_paths.append(ref)
    confirmed_todo = any(source.get("kind") in todo_kinds for source in confirmed)
    semantic_sources: dict[str, str] = {}
    for source in confirmed:
        source_id = source.get("source_id")
        source_revision = source.get("revision")
        kind = source.get("kind")
        ref = source.get("ref")
        if not all(isinstance(value, str) and value for value in (source_id, source_revision, kind, ref)):
            continue
        semantic = semantic_source_revision(
            repo=repo,
            kind=kind,
            ref=ref,
            source_id=source_id,
            revision=source_revision,
            status=source.get("status") if isinstance(source.get("status"), str) else None,
        )
        if semantic is None:
            continue
        key, value = semantic
        previous = semantic_sources.setdefault(key, value)
        if previous != value:
            raise AuthorityValidationError(
                "confirmed semantic work authority revisions conflict",
                reason_code=REASON_ROW_MALFORMED,
                repo=repo_label,
                work_id=work_id_label,
                field="source_revisions",
            )
    source_revisions = tuple(
        f"{source_id}@{semantic_sources[source_id]}" for source_id in sorted(semantic_sources)
    )
    if not source_revisions:
        raise AuthorityValidationError(
            "confirmed work authority revisions invalid",
            reason_code=REASON_ROW_MALFORMED,
            repo=repo_label,
            work_id=work_id_label,
            field="source_revisions",
        )
    return WorkAuthority._verified(
        repo=repo,
        work_id=work_id,
        mapped_issues=tuple(sorted(set(issues))),
        mapped_prs=tuple(sorted(set(prs))),
        mapped_openspec=tuple(sorted(set(changes))),
        mapped_todo_paths=tuple(sorted(set(todo_paths))),
        confirmed_todo=confirmed_todo,
        # R0.5 D1：auto label 改由 monitor 鏡像判定（GitHubWorkProvider 把持有
        # `cortex:auto-on-going` 的 open issue 編號寫進 provider observations），
        # 取代先前 canonical 路徑硬編 False＋manager 每 tick 逐 issue live 讀取。
        # observations 缺失或形狀不合一律保守 False——auto 派工少跑一輪無害，
        # 誤跑才有害；且 auto-claim 在真正 claim 前仍會做一次 targeted 複驗。
        auto_label=_auto_label_from_observations(github, issues),
        source_revisions=source_revisions,
        provider_revision=revision,
        provider_id=provider_id,
        last_success_epoch=last_success,
        snapshot_hash=snapshot_hash,
        requires_github_authority=requires_github_authority,
    )


def _safe_todo_path(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    pure = PurePosixPath(value)
    return (
        not pure.is_absolute()
        and ".." not in pure.parts
        and pure.as_posix() == value
        and pure.suffix.lower() == ".md"
    )


def _authority_digest_payload(
    authority: WorkAuthority, source_revisions: tuple[str, ...]
) -> dict:
    return {
        "repo": authority.repo,
        "work_id": authority.work_id,
        "provider_id": authority.github_provider_id,
        "source_revisions": list(source_revisions),
        "mapped_issues": list(authority.mapped_issues),
        "mapped_prs": list(authority.mapped_prs),
        "mapped_openspec": list(authority.mapped_openspec),
        "mapped_todo_paths": list(authority.mapped_todo_paths),
        "confirmed_todo": authority.confirmed_todo,
    }


def work_authority_digest(authority: WorkAuthority) -> str:
    if not isinstance(authority, WorkAuthority):
        raise ValueError("confirmed WorkAuthority is required")
    return verification.canonical_json_hash(
        _authority_digest_payload(authority, authority.source_revisions)
    )


# #524：monitor 的 repo provider 以 glob 掃 `docs/superpowers/specs/**/*.md` 與
# `docs/superpowers/plans/**/*.md` 產生的 source id 前綴（見 monitor/providers.py）。
#
# 這兩類 source 依構造永遠是 **planning phase 自己的產出**，不可能是 operator 在
# `.cortex/work-items.yaml` 宣告的授權來源——canonical row 解析
# （`_authority_from_canonical_row`）只認 `github_issue`／`github_pr`／`openspec`／
# `todo` 四種 kind，`superpowers_*` 完全不在其中，只會被 monitor 掃出來。因此它們
# 在 authority 裡「出現」這件事，只代表該 work item 的 run 正在成功推進，不代表
# authority 被任何外部事實改動過。
PLANNING_OUTPUT_SOURCE_PREFIXES = ("superpowers_spec:", "superpowers_plan:")


def authority_digest_without_planning_outputs(authority: WorkAuthority) -> str:
    """把 planning phase 自產的 source 剝掉之後重算的 authority digest。

    #524：`claim_key`／`run.source_revision` 都由 `work_authority_digest` 導出，
    而該 digest 折入 `source_revisions`。run 的 brainstorming／writing-plans 卡一旦
    把 spec/design/plan 寫進 governed roots，monitor 下一輪就把它們當成新的
    confirmed source 併進同一個 work item——digest 因此改變，run 的持久化識別與
    「目前 authority 算出來的識別」再也對不上，claim 路徑於是把仍在 flight 的 run
    當成陳舊世代作廢。

    本函式提供的是「若不算 run 自己的產出，authority 是否仍是 claim 當下那一份」
    這個判準：與 `run.source_revision` 相等即代表**整段漂移都是自己造成的**，此時
    不得換代。反之（issue 開關、openspec revision、todo 成員變動……）維持既有的
    新世代語意，operator 明確 `start` 換代的逃生口不受影響。

    生產現場驗證：以 2026-08-14 的 snapshot 剝除兩個 `superpowers_spec` 與一個
    `superpowers_plan` source 後重算，digest 為
    `039e89aab0a56384bce29bc89dc638c4e176f96873e9a4d89627b223d79a31bf`，與被誤
    supersede 的 `workflow-009fe9ab303df196209d` 持久化的 `source_revision` 逐字
    相符。
    """

    if not isinstance(authority, WorkAuthority):
        raise ValueError("confirmed WorkAuthority is required")
    kept = tuple(
        revision
        for revision in authority.source_revisions
        if not revision.startswith(PLANNING_OUTPUT_SOURCE_PREFIXES)
    )
    return verification.canonical_json_hash(_authority_digest_payload(authority, kept))


def claim_identity_digest(authority: WorkAuthority) -> str:
    """Stable claim identity, excluding the planning-artifact-driven fields
    (#213, design #208 A.1: freeze point moves to *after* plan review passes).

    ``work_authority_digest`` folds in ``mapped_openspec``/``mapped_todo_paths``/
    ``source_revisions`` — exactly the fields a plan -> plan review -> revision
    loop touches as planning artifacts are drafted and rewritten. Comparing
    against the *full* digest while ``planning.plan_review_gate`` has not yet
    returned ``ready=True`` makes every plan revision look like a changed
    authority, so ``_existing()`` treats an active workflow as unmatched and
    the caller mints a fresh claim — the mechanism behind hippo #18's #3/#7
    v3->v4->... authority generation growth. This digest is the light,
    GitHub-anchored identity (``mapped_issues``/``mapped_prs``/``confirmed_todo``)
    a plan revision alone cannot change, used by ``_existing()`` while an
    active run's plan review has not passed yet.
    """
    if not isinstance(authority, WorkAuthority):
        raise ValueError("confirmed WorkAuthority is required")
    payload = {
        "repo": authority.repo,
        "work_id": authority.work_id,
        "provider_id": authority.github_provider_id,
        "mapped_issues": list(authority.mapped_issues),
        "mapped_prs": list(authority.mapped_prs),
        "confirmed_todo": authority.confirmed_todo,
    }
    return verification.canonical_json_hash(payload)


def _load_work_authorities_with_diagnostics(
    *,
    snapshot_path: str | Path | None = None,
    allow_rate_limited_last_known_good: bool = False,
) -> tuple[tuple[WorkAuthority, ...], tuple[AuthorityValidationError, ...]]:
    """Parse every row independently (#206 AC4): one row's validation failure
    is recorded as ``AuthorityValidationError`` diagnostics and the row is
    dropped from the result, but parsing continues for the remaining rows —
    an unrelated repo's degraded/malformed provider must never blast-radius
    a healthy repo's work-action (durable "authority invalid" recurrence).

    Fail-closed is preserved: a skipped row's work item simply never appears
    in the returned authorities, so any lookup for it still fails — just
    with the specific skip reason (see ``load_work_authority``) instead of
    aborting the whole snapshot load.
    """
    payload, digest = _load_snapshot(snapshot_path)
    providers = payload["providers"]
    parsed: list[WorkAuthority] = []
    skipped: list[AuthorityValidationError] = []
    for row in payload["work_items"]:
        try:
            authority = _authority_from_row(
                row=row,
                providers=providers,
                snapshot_hash=digest,
                allow_rate_limited_last_known_good=allow_rate_limited_last_known_good,
            )
        except AuthorityValidationError as exc:
            skipped.append(exc)
            continue
        if authority is not None:
            parsed.append(authority)
    authorities = tuple(parsed)
    identities = [(authority.repo, authority.work_id) for authority in authorities]
    if len(set(identities)) != len(identities):
        raise ValueError("confirmed work authority missing or ambiguous")
    # Source-owner transfers (#217, design #208 D) move an issue's mapped_issues
    # from one work_id to another. If the durable snapshot is ever read back
    # while two different work_ids both confirm the same issue — the mid-
    # transfer state that must never surface — refuse rather than silently
    # picking a "winner": every claim/ship/abandon caller loads authority
    # through here, so this closes the ambiguity at the single choke point.
    # These two integrity checks are snapshot-wide invariants, not per-row
    # parsing failures, so they intentionally keep the pre-#206 raise
    # behaviour rather than joining the per-row isolation above.
    owners: dict[tuple[str, int], str] = {}
    for authority in authorities:
        for issue in authority.mapped_issues:
            key = (authority.repo, issue)
            owner = owners.setdefault(key, authority.work_id)
            if owner != authority.work_id:
                raise ValueError("confirmed work authority missing or ambiguous")
    return authorities, tuple(skipped)


def load_work_authorities(
    *, snapshot_path: str | Path | None = None
) -> tuple[WorkAuthority, ...]:
    authorities, _skipped = _load_work_authorities_with_diagnostics(snapshot_path=snapshot_path)
    return authorities


def load_work_authority(
    *,
    repo: str,
    work_id: str,
    snapshot_path: str | Path | None = None,
    allow_rate_limited_last_known_good: bool = False,
) -> WorkAuthority:
    """Load one confirmed WorkAuthority.

    ``allow_rate_limited_last_known_good`` (default ``False``, i.e. strict
    fail-closed) is opt-in for the *retirement* context only (see
    ``work_actions._RETIREMENT_ACTIONS``): when the canonical GitHub provider
    is degraded specifically by a rate limit but still carries a prior
    last-known-good ``revision``/``last_success_at``, the authority is built
    from that snapshot instead of raising, so a stuck local run can still be
    torn down while the provider is throttled. Every other caller keeps the
    strict default and needs fresh authority.
    """

    authorities, skipped = _load_work_authorities_with_diagnostics(
        snapshot_path=snapshot_path,
        allow_rate_limited_last_known_good=allow_rate_limited_last_known_good,
    )
    matches = [
        authority
        for authority in authorities
        if authority.repo == repo and authority.work_id == work_id
    ]
    if len(matches) == 1:
        return matches[0]
    # #206 AC1/C：目標本身就是被跳過的壞 row → 拋出帶該 row reason code 的錯誤，
    # 而不是泛化的 missing/ambiguous，讓呼叫端能診斷「因為它的 authority 無效」。
    for exc in skipped:
        if exc.repo == repo and exc.work_id in (None, work_id):
            raise exc
    payload, _ = _load_snapshot(snapshot_path)
    if isinstance(payload, dict) and payload.get("last_refresh_error"):
        raise ValueError(
            f"confirmed work authority missing or ambiguous (monitor refresh failed: {payload['last_refresh_error']})"
        )
    raise ValueError("confirmed work authority missing or ambiguous")


@dataclass(frozen=True)
class ClaimCandidate:
    authority: WorkAuthority
    repo: str
    work_id: str
    source_revisions: tuple[str, ...]
    confirmed_todo: bool
    confirmed_issue: int | None
    auto_label: bool
    active_run_id: str | None
    active_claim_key: str | None
    active_status: str | None = None
    active_snapshot_hash: str | None = None
    active_source_revisions: tuple[str, ...] | None = None
    active_provider_revision: str | None = None
    active_authority_digest: str | None = None
    active_plan_review_passed: bool = True
    active_claim_identity_digest: str | None = None
    # #256 R2：resume 對 needs_human 必須說得出「為什麼卡住、現在能做什麼」。
    # 以下三個欄位是那個判斷的唯一輸入，全部取自系統寫入的 run 狀態／evidence
    # （current_phase 與 planning failure record），呼叫端自述不得寫入。
    active_phase: str | None = None
    active_planning_failure_classification: str | None = None
    active_planning_failure_reason: str | None = None


@dataclass(frozen=True)
class ClaimDecision:
    action: str
    reason: str | None = None
    claim_key: str | None = None
    run_id: str | None = None
    next_actions: tuple[str, ...] = ()
    # #256 R2：`reason` 是動作語意（呼叫端據以分支，維持穩定）；`blocking_reason`
    # 才是「這個 run 到底為什麼停住」的具體原因，取自 run 自己的 evidence，
    # 拿不到時為 None（不編造）。
    blocking_reason: str | None = None


def _validate_candidate(candidate: ClaimCandidate) -> None:
    repo_valid = re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", candidate.repo)
    work_id_valid = re.fullmatch(r"[a-z0-9][a-z0-9-]*", candidate.work_id)
    if repo_valid is None or work_id_valid is None:
        raise ValueError("claim candidate repo/work_id invalid")
    if not isinstance(candidate.authority, WorkAuthority):
        raise ValueError("confirmed WorkAuthority is required")
    if candidate.repo != candidate.authority.repo or candidate.work_id != candidate.authority.work_id:
        raise ValueError("claim candidate does not match WorkAuthority")
    for field, value in (
        ("confirmed_todo", candidate.confirmed_todo),
        ("auto_label", candidate.auto_label),
        ("active_plan_review_passed", candidate.active_plan_review_passed),
    ):
        if not isinstance(value, bool):
            raise ValueError(f"{field} must be boolean")
    if (
        candidate.active_run_id is None
        and candidate.confirmed_todo is not candidate.authority.confirmed_todo
    ):
        raise ValueError("claim Todo flag does not match WorkAuthority")
    if not candidate.source_revisions or any(
        not isinstance(revision, str) or not revision.strip()
        for revision in candidate.source_revisions
    ):
        raise ValueError("source revisions must be non-empty strings")
    if (
        candidate.active_run_id is None
        and tuple(sorted(candidate.source_revisions)) != candidate.authority.source_revisions
    ):
        raise ValueError("claim revisions do not match WorkAuthority")
    if candidate.confirmed_issue is not None and (
        not isinstance(candidate.confirmed_issue, int)
        or isinstance(candidate.confirmed_issue, bool)
        or candidate.confirmed_issue <= 0
    ):
        raise ValueError("confirmed_issue must be a positive integer or null")
    if candidate.confirmed_issue is not None and candidate.confirmed_issue not in candidate.authority.mapped_issues:
        raise ValueError("confirmed_issue is not authorized by WorkAuthority")
    if candidate.active_run_id is None and candidate.active_claim_key is not None:
        raise ValueError("active_claim_key requires active_run_id")
    if candidate.active_planning_failure_classification is not None and (
        candidate.active_planning_failure_classification not in {"environment", "content"}
    ):
        raise ValueError("active planning failure classification invalid")
    if candidate.active_planning_failure_reason is not None and (
        not isinstance(candidate.active_planning_failure_reason, str)
        or not candidate.active_planning_failure_reason.strip()
    ):
        raise ValueError("active planning failure reason must be a non-empty string")
    if (
        candidate.active_planning_failure_classification is None
        and candidate.active_planning_failure_reason is not None
    ):
        raise ValueError("planning failure reason requires its classification")
    if candidate.active_run_id is not None:
        if not isinstance(candidate.active_run_id, str) or not candidate.active_run_id.strip():
            raise ValueError("active_run_id must be a non-empty string")
        if (
            not isinstance(candidate.active_claim_key, str)
            or not candidate.active_claim_key.startswith("claim:v1:")
            or len(candidate.active_claim_key) != len("claim:v1:") + 64
            or any(ch not in "0123456789abcdef" for ch in candidate.active_claim_key[-64:])
        ):
            raise ValueError("active workflow requires its persisted claim key")
        if candidate.active_status not in {
            "ongoing",
            "needs_human",
            "blocked",
            "done",
            "needs_decomposition",
        }:
            raise ValueError("active workflow status invalid")
        if (
            not isinstance(candidate.active_snapshot_hash, str)
            or len(candidate.active_snapshot_hash) != 64
            or candidate.active_source_revisions is None
            or not candidate.active_source_revisions
            or not isinstance(candidate.active_provider_revision, str)
            or not candidate.active_provider_revision
            or not isinstance(candidate.active_authority_digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", candidate.active_authority_digest) is None
        ):
            raise ValueError("active workflow authority metadata missing")
        if not candidate.active_plan_review_passed and (
            not isinstance(candidate.active_claim_identity_digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", candidate.active_claim_identity_digest) is None
        ):
            # #213：plan review 尚未通過（freeze 未發生）時，_existing() 改用
            # claim_identity_digest 比對，這個欄位就是它比對的持久化基準。
            raise ValueError("active workflow pre-freeze identity digest missing")


def claim_key_for_authority_digest(*, repo: str, work_id: str, authority_digest: str) -> str:
    """Deterministic ``claim:v1:...`` key for an exact (repo, work_id,
    authority_digest) triple.

    Factored out of ``build_claim_key`` (#373) so an in-place authority-digest
    rewrite — ``registry._manager_reset_workflow_for_authority_restart`` —
    can re-derive the same key a fresh claim would produce for that digest,
    without needing the full ``WorkAuthority`` object. Keeping ``claim_key``
    in sync with ``source_revision`` after an authority restart is the fix
    for #373: previously the reset only rewrote ``source_revision``, leaving
    ``claim_key`` permanently mismatched against
    ``work_actions._expected_claim_key(authority)`` — every subsequent
    automatic scan tick re-triggered the same reset, stripping the
    ``needs_human`` facet and re-raising the workflow job binding mismatch
    forever.
    """
    payload = {"repo": repo, "work_id": work_id, "authority_digest": authority_digest}
    digest = verification.canonical_json_hash(payload)
    return f"claim:v1:{digest}"


def build_claim_key(candidate: ClaimCandidate) -> str:
    _validate_candidate(candidate)
    if not candidate.source_revisions:
        raise ValueError("new claim requires authoritative source revisions")
    return claim_key_for_authority_digest(
        repo=candidate.repo,
        work_id=candidate.work_id,
        authority_digest=work_authority_digest(candidate.authority),
    )


def _existing(candidate: ClaimCandidate) -> ClaimDecision | None:
    if candidate.active_run_id is None:
        return None
    if not candidate.active_plan_review_passed:
        # #213（design #208 A.1）：freeze point 位於 plan review 通過之後。
        # 這個 run 的 plan 仍在 plan -> revision 迴圈裡（尚未 freeze），只比對
        # 穩定 identity（claim_identity_digest，不含 mapped_openspec/
        # mapped_todo_paths/source_revisions）——plan 修訂造成這些欄位飄移不算
        # authority 變更，不觸發 supersede、不生出新世代（hippo #18 #3/#7）。
        # 持久化的 claim_key 是在 plan 存在之前鎖定的，此時不得拿（帶有目前飄移
        # 欄位的）完整 digest 反向驗證它，所以不做 expected_key 比對。
        if candidate.active_claim_identity_digest != claim_identity_digest(candidate.authority):
            return None
        return _resume_decision(candidate)
    authority_changed = (
        candidate.active_authority_digest != work_authority_digest(candidate.authority)
        or tuple(sorted(candidate.active_source_revisions or ()))
        != candidate.authority.source_revisions
    )
    if authority_changed:
        return None
    expected_key = build_claim_key(
        replace(
            candidate,
            active_run_id=None,
            active_claim_key=None,
            active_status=None,
            active_snapshot_hash=None,
            active_source_revisions=None,
            active_provider_revision=None,
            active_authority_digest=None,
        )
    )
    if candidate.active_claim_key != expected_key:
        raise ValueError("persisted claim key does not match authority")
    return _resume_decision(candidate)


def needs_human_next_actions(
    *,
    phase: str | None,
    planning_failure_classification: str | None,
) -> tuple[str, ...]:
    """`needs_human` run 的基礎合法動作集合——**單一導出點，回傳值永不為空**。

    #256 R2 的判準原本只長在 `_resume_decision` 裡；`cortex status` 的 attention
    投影（`manager.workflow_status_entry`）另走 `work_actions.
    _phase_recovery_actions` 一條，而後者只涵蓋 build／verify／review 三個 phase
    （`registry.RETRY_CARD_PHASE_PERSONA`）。兩份導出漂移的後果就是 #728 的現場：
    `plan` phase 的 needs_human run（`planning-authority-reconciliation-failed`）
    拿到 `next_actions: []`——fail-closed 可以，**無出路不行**。

    判準本身不變，只是被抬成兩側共用的函式：

    - `abandon` **永遠**合法（#256 R3：釋放後可重 claim），因此本函式不可能回空
      集合；這就是「至少給得出一個合法動作」的機械保證。
    - `recover-planning` 只在「停在 `define` 的環境類 planning 失敗」才浮現
      （R1 fail-closed），與 `work_actions._recover_planning_action` 自身的前置驗
      （`run.current_phase != "define"` 與 `classification != "environment"` 兩條
      拒收）同一組條件——宣告一個保證失敗的動作比不宣告更糟（#382）。
    """

    if planning_failure_classification == "environment" and phase == "define":
        return ("recover-planning", "abandon")
    return ("abandon",)


def _resume_decision(candidate: ClaimCandidate) -> ClaimDecision:
    if candidate.active_status == "done":
        return ClaimDecision(
            action="done",
            reason="already-completed",
            claim_key=candidate.active_claim_key,
            run_id=candidate.active_run_id,
            next_actions=(),
        )
    if candidate.active_status == "needs_human":
        # #256 R2：不得只原樣回報狀態。`abandon` 永遠合法（釋放後可重 claim，R3）；
        # `recover-planning` 只有在該 run 自己的 evidence 顯示「停在 define 的
        # 環境類 planning 失敗」時才是合法出口——內容類失敗不得由本路徑繞過
        # （R1 fail-closed），拿不到 evidence 時也不宣稱它可用。
        classification = candidate.active_planning_failure_classification
        # #728：判準抬進 `needs_human_next_actions`，讓 attention 投影
        # （`manager.workflow_status_entry`）讀**同一個**函式而不是自己再寫一份。
        next_actions = needs_human_next_actions(
            phase=candidate.active_phase,
            planning_failure_classification=classification,
        )
        blocking_reason = (
            f"planning-failure:{classification}:{candidate.active_planning_failure_reason}"
            if classification is not None and candidate.active_planning_failure_reason
            else None
        )
        return ClaimDecision(
            action="needs_human",
            reason="human-intervention-required",
            claim_key=candidate.active_claim_key,
            run_id=candidate.active_run_id,
            next_actions=next_actions,
            blocking_reason=blocking_reason,
        )
    if candidate.active_status == "blocked":
        return ClaimDecision(
            action="blocked",
            reason="persisted-block",
            claim_key=candidate.active_claim_key,
            run_id=candidate.active_run_id,
            next_actions=("abandon",),
        )
    if candidate.active_status == "needs_decomposition":
        # #223（design #208 H.3）：run 已因 Red band 轉入拆分路由（見
        # workflow_status()／manager._dispatch_workflow_card）。resume 掃描
        # 不得把它當成一般 in-flight run 續跑，必須原樣浮現給呼叫端另行處理
        # （回派 planner 拆分或人工介入），不得以原身分繼續重試。
        return ClaimDecision(
            action="needs_decomposition",
            reason="decomposition-required",
            claim_key=candidate.active_claim_key,
            run_id=candidate.active_run_id,
            next_actions=(),
        )
    return ClaimDecision(
        action="resume",
        reason="active-workflow",
        claim_key=candidate.active_claim_key,
        run_id=candidate.active_run_id,
        next_actions=(),
    )


def _authority_is_fresh(authority: WorkAuthority, *, now_epoch: int | float) -> bool:
    if (
        not isinstance(now_epoch, (int, float))
        or isinstance(now_epoch, bool)
        or not math.isfinite(float(now_epoch))
    ):
        raise ValueError("claim clock must be finite")
    # #530：新鮮度檢查衡量的是「GitHub 觀測有多舊」。權威不依賴 GitHub 的 work
    # item（sources 全部來自本機檔案系統 provider）沒有這個維度可言——它的內容新鮮度
    # 已由 `source_revisions` 承載，拿 GitHub 的 last-success 時間去擋它是錯配。
    # 這是 `#530` 三層放大中的第二層；只修 `_authority_from_canonical_row` 而不修
    # 這裡，claim 仍會在 decide_manual_start／auto-claim 被擋下。
    if not getattr(authority, "requires_github_authority", True):
        return True
    age = float(now_epoch) - authority.github_last_success_epoch
    return 0 <= age <= PROVIDER_MAX_AGE_SECONDS


def decide_manual_start(
    candidate: ClaimCandidate,
    *,
    now_epoch: int | float,
) -> ClaimDecision:
    _validate_candidate(candidate)
    existing = _existing(candidate)
    if existing is not None:
        return existing
    if not _authority_is_fresh(candidate.authority, now_epoch=now_epoch):
        return ClaimDecision(action="blocked", reason="provider-degraded-or-stale")
    if not candidate.confirmed_todo:
        return ClaimDecision(action="refuse", reason="confirmed-todo-required")
    if candidate.confirmed_issue is None:
        return ClaimDecision(action="needs_human", reason="missing_issue")
    return ClaimDecision(action="claim", claim_key=build_claim_key(candidate))


def decide_auto_claim(
    candidate: ClaimCandidate,
    *,
    now_epoch: int | float,
) -> ClaimDecision:
    _validate_candidate(candidate)
    existing = _existing(candidate)
    if existing is not None:
        return existing
    if not _authority_is_fresh(candidate.authority, now_epoch=now_epoch):
        return ClaimDecision(action="blocked", reason="provider-degraded-or-stale")
    if not candidate.confirmed_todo:
        return ClaimDecision(action="ignore", reason="confirmed-todo-required")
    if candidate.confirmed_issue is None:
        return ClaimDecision(action="needs_human", reason="missing_issue")
    if not candidate.auto_label:
        return ClaimDecision(action="ignore", reason="auto-label-missing")
    return ClaimDecision(action="claim", claim_key=build_claim_key(candidate))


def build_label_argv(*, repo: str, issue: int, enabled: bool) -> list[str]:
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) is None:
        raise ValueError("repo must be owner/name")
    if not isinstance(issue, int) or isinstance(issue, bool) or issue <= 0:
        raise ValueError("issue must be a positive integer")
    if enabled:
        return [
            "gh",
            "api",
            "--method",
            "POST",
            f"repos/{repo}/issues/{issue}/labels",
            "-f",
            f"labels[]={AUTO_LABEL}",
        ]
    return [
        "gh",
        "api",
        "--method",
        "DELETE",
        f"repos/{repo}/issues/{issue}/labels/{quote(AUTO_LABEL, safe='')}",
    ]


# --- #222（design #208 H.2）：五維 sizing 總分 → band 判定 -------------------
#
# band 字串沿用 deck.schema.BAND_LEVELS（green/yellow/red），不得另立常數或大小
# 寫變體。閾值 Green 0–3／Yellow 4–6／Red 7–10，對應 planning.SizingScore.total
# （五維、每維 0–2、總分 0–10，見 #221）。claim.py／registry.py／completion.py
# 三處共用這份純函式，避免各自硬編碼門檻造成漂移。band 本身只負責重算與記錄
# （#222）；跨帶上升後的拆分「路由」屬 #223，不在本模組範圍。
SIZING_BAND_GREEN_MAX = 3
SIZING_BAND_YELLOW_MAX = 6


def sizing_band(total: int) -> str:
    """五維 sizing 總分（0–10）→ band。呼叫端每次 repair／re-claim 都須重新
    傳入當下算出的 total，不得沿用 claim 當時判定的舊值（#222 驗收條件 3）。
    """
    if not isinstance(total, int) or isinstance(total, bool) or not (0 <= total <= 10):
        raise ValueError("sizing total 必須為 0–10 的整數")
    if total <= SIZING_BAND_GREEN_MAX:
        return BAND_LEVELS[0]
    if total <= SIZING_BAND_YELLOW_MAX:
        return BAND_LEVELS[1]
    return BAND_LEVELS[2]


# --- #223（design #208 H.3）：Red band 拆分路由 -----------------------------
#
# sizing_band()=='red' 的 work item 不得直接進 build，也不得帶著原 run 身分
# 繼續重試（#223 驗收條件 4）：收斂路徑是 needs_decomposition（回派 planner
# 拆分，拆分屬 Yellow 級 planning 工作，在 planner 封套內，design #208 原文）。
# 拆分深度（WorkflowRun.decomposition_depth，根 work item 為 0）每拆一層 +1，
# 上限 DECOMPOSITION_DEPTH_LIMIT 層；逾限改轉 needs_human，不得無限拆分下去
# （#223 驗收條件 3）。呼叫端（manager._dispatch_workflow_card 的 plan phase
# 完成掛載點）只需傳入目前的 decomposition_depth，不必自行重複這條門檻判定。
DECOMPOSITION_DEPTH_LIMIT = 2


def decomposition_route(*, decomposition_depth: int) -> str:
    """Red band 的路由決策：回傳 ``"needs_decomposition"`` 或 ``"needs_human"``。

    只在呼叫端已確認 ``sizing_band(total) == "red"`` 時呼叫；Green/Yellow 不
    經過此函式。
    """
    if (
        not isinstance(decomposition_depth, int)
        or isinstance(decomposition_depth, bool)
        or decomposition_depth < 0
    ):
        raise ValueError("decomposition_depth 必須為非負整數")
    if decomposition_depth >= DECOMPOSITION_DEPTH_LIMIT:
        return "needs_human"
    return "needs_decomposition"
