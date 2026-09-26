"""#384：executor/provider 失敗的 typed 分類與 bounded recovery 的共用語意。

Root cause（見 issue #384 2026-08-10 複驗 comment）：`completion.classify_completion`
只有 exited/failed 兩值，job registry 也只有 status/exit_code；下游（slice lane
`manager.py` 的 build-phase failure、workflow lane 的 job-failed 分支）因此一律
把任何 executor 失敗壓平成寫死的 ``"builder-failed"``／``"job-failed"``＋
``needs_human``——auth 失效、rate limit、暫時性網路錯誤、內容政策拒答，全部
得到同一種（無分類、無 retry、無 backoff）處置。

本模組不重造分類器：複用 #369 的 :func:`executor_auth.classify_cli_output`
（rate_limited／logged_out／ok／unknown，且已修好 rate-limit 必須先於 login
判定的順序）與 #370 的 :mod:`paulsha_cortex.github_rate_limit`（獨立的
rate-limit／auth 訊號正則，覆蓋 executor_auth 未涵蓋的措辭），在其上疊
quota／transient／content 三類新訊號，統一成一個
:class:`ProviderFailureClassification`。

**中間 authority 等級**（解 issue 內註記的 plan 矛盾——規則 5「stderr 關鍵字只做
hint 不升 authoritative」vs. recovery matrix 期待 rate_limited 在 copilot
這種「限流只有 stderr 文字、無 structured code」的 executor 上仍能觸發 retry）：

- :data:`SignalAuthority.STRUCTURED` —— 來自結構化訊號（明確 HTTP status
  code、JSON 錯誤欄位）。可驅動任何決策，含 policy 層決策。
- :data:`SignalAuthority.TEXT_SIGNAL` —— 來自 CLI stdout/stderr 文字關鍵字比對
  （本模組與 executor_auth／github_rate_limit 的既有 regex）。比 HINT
  強——足以驅動**有界、可逆**的動作（這一輪 bounded retry、在既有 candidate
  順序上 re-route），但**不足以**驅動不可逆或 policy 層決策（放寬
  independence domain、永久拉黑 provider、略過人工複核）。這正是規則 5 真正想
  擋的：不是「stderr 訊號不能用」，是「stderr 訊號不能升級到需要 structured
  authority 的動作」。retry／re-route 從未要求 structured authority，故兩者
  不矛盾。
- :data:`SignalAuthority.HINT` —— exit code 非零但無任何已知訊號匹配。只供
  人類判讀，不驅動任何自動決策（既不 retry 也不變更 gate_reason 之外的欄位）。

**#499／#500／#487（2026-08-15）**：分類器本身移交
:mod:`paulsha_cortex.coordinator.outcome_taxonomy`——三個 lane 共用同一份
markers 表與同一套證據分層，本模組只保留 build lane 的 outcome 詞彙
（:class:`ProviderOutcome`）與 authority 分級。三個實質修正隨之落地：

- 結構化終局證據優先於文字關鍵字（#499：`rate_limit_event` / 429 終局狀態）。
- nested tool result 與 init metadata 不再是分類證據（#500 / #487）。
- rate-limit 帶回 provider 給的權威重置時刻（:attr:`ProviderFailureClassification.reset_at`）。

#582 另將精確的 ``error_during_execution``／``aborted_tools`` 終局分類為
``tool_aborted``：family 為 environment，且允許有界重試。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Mapping

from . import outcome_taxonomy

__all__ = [
    "ProviderOutcome",
    "SignalAuthority",
    "RETRYABLE_OUTCOMES",
    "ProviderFailureClassification",
    "classify_provider_failure",
    "classify_launch_failure",
    "classification_from_job",
    "read_log_tail",
]


class ProviderOutcome(str, Enum):
    """executor/provider 失敗的分類結果。"""

    AUTH = "auth"
    RATE_LIMITED = "rate_limited"
    QUOTA = "quota"
    EFFORT_NOT_SUPPORTED = "effort_not_supported"
    EXECUTABLE_NOT_FOUND = "executable_not_found"
    LAUNCH_FAILED = "launch_failed"
    TOOL_ABORTED = "tool_aborted"
    TRANSIENT = "transient"
    CONTENT = "content"
    UNKNOWN = "unknown"


class SignalAuthority(str, Enum):
    """分類結果的可信等級——決定這個結果可以驅動多強的決策（見模組 docstring）。"""

    STRUCTURED = "structured"
    TEXT_SIGNAL = "text_signal"
    HINT = "hint"


# 這些類別有足夠訊號支援 bounded retry：rate limit 會隨時間窗重置、transient
# 是網路/服務暫時性錯誤，tool_aborted 則是工具鏈被外部生命週期中斷。auth／
# content／quota／unknown 不應重試原 identity（auth 需要人工重新登入；content
# 是模型對這個 prompt 的決定；quota 換到 roster 中下一個 identity 才可能繼續；
# unknown 沒有訊號可支持任何自動決策）。
RETRYABLE_OUTCOMES = frozenset(
    {ProviderOutcome.RATE_LIMITED, ProviderOutcome.TRANSIENT, ProviderOutcome.TOOL_ABORTED}
)
_REROUTABLE_OUTCOMES = frozenset(
    {
        ProviderOutcome.EFFORT_NOT_SUPPORTED,
        ProviderOutcome.EXECUTABLE_NOT_FOUND,
        ProviderOutcome.QUOTA,
    }
)
_KNOWN_PROVIDER_EXECUTABLE_TOKENS = frozenset({"agy", "cg", "claude", "codex", "copilot"})
_SHARED_LAUNCH_INFRASTRUCTURE_TOKENS = frozenset(
    {
        "ash",
        "bash",
        "dash",
        "fish",
        "git",
        "ksh",
        "powershell",
        "pwsh",
        "sh",
        "systemctl",
        "systemd-run",
        "zsh",
    }
)

# 分類結果 payload 的必要鍵。`reset_at` 是可選鍵（#499：只有帶得到權威重置
# 時刻的 rate-limit 才會有），故舊狀態檔的四鍵 payload 仍原樣可讀。
_PROVIDER_OUTCOME_FIELDS = frozenset({"outcome", "authority", "reason", "retryable"})
_PROVIDER_OUTCOME_OPTIONAL_FIELDS = frozenset({"reset_at"})

# markers 表已移交 outcome_taxonomy（#499／#500／#487／#485：三個 lane 共用
# 單一真源）。以下對照表把 taxonomy 的細分訊號翻回本 lane 的詞彙。
_OUTCOME_BY_TEXT_SIGNAL: dict[outcome_taxonomy.TextSignal, ProviderOutcome] = {
    outcome_taxonomy.TextSignal.RATE_LIMIT: ProviderOutcome.RATE_LIMITED,
    outcome_taxonomy.TextSignal.QUOTA: ProviderOutcome.QUOTA,
    outcome_taxonomy.TextSignal.AUTH: ProviderOutcome.AUTH,
    outcome_taxonomy.TextSignal.EFFORT_NOT_SUPPORTED: ProviderOutcome.EFFORT_NOT_SUPPORTED,
    outcome_taxonomy.TextSignal.EXECUTABLE_NOT_FOUND: ProviderOutcome.EXECUTABLE_NOT_FOUND,
    outcome_taxonomy.TextSignal.CONTENT: ProviderOutcome.CONTENT,
    outcome_taxonomy.TextSignal.TRANSIENT: ProviderOutcome.TRANSIENT,
    outcome_taxonomy.TextSignal.NONE: ProviderOutcome.UNKNOWN,
}

# 結構化訊號 → 本 lane 詞彙。`INTERRUPTED` 落 UNKNOWN：controller 中斷既非
# provider 失敗也非模型內容缺陷，維持既有「不自動重試」處置（#500 要的正是
# 不要再把它當成 transient 排重試）。
_OUTCOME_BY_STRUCTURED_KIND: dict[outcome_taxonomy.StructuredKind, ProviderOutcome] = {
    outcome_taxonomy.StructuredKind.RATE_LIMITED: ProviderOutcome.RATE_LIMITED,
    outcome_taxonomy.StructuredKind.TRANSIENT: ProviderOutcome.TRANSIENT,
    outcome_taxonomy.StructuredKind.AUTH: ProviderOutcome.AUTH,
    outcome_taxonomy.StructuredKind.EXECUTABLE_NOT_FOUND: ProviderOutcome.EXECUTABLE_NOT_FOUND,
    outcome_taxonomy.StructuredKind.LAUNCH_FAILED: ProviderOutcome.LAUNCH_FAILED,
    outcome_taxonomy.StructuredKind.TOOL_ABORTED: ProviderOutcome.TOOL_ABORTED,
    outcome_taxonomy.StructuredKind.INTERRUPTED: ProviderOutcome.UNKNOWN,
}


@dataclass(frozen=True)
class ProviderFailureClassification:
    """一次 executor 失敗的分類結果（純資料）。"""

    outcome: ProviderOutcome
    authority: SignalAuthority
    reason: str
    # #499：provider 給的權威重置時刻（epoch 秒），目前只有結構化限流證據
    # （Claude `rate_limit_event.resetsAt`）帶得到。None 時 `to_dict()` 不寫這個
    # 鍵，舊讀取端與舊狀態檔的四鍵形狀完全不受影響。
    reset_at: int | None = None
    reset_parser: dict[str, object] | None = field(default=None, compare=False)

    @property
    def retryable(self) -> bool:
        """是否適合驅動 bounded retry——見模組 docstring 的 authority 分級。"""

        return self.authority is not SignalAuthority.HINT and self.outcome in RETRYABLE_OUTCOMES

    @property
    def reroutable(self) -> bool:
        """是否適合驅動 bounded re-route（不持久化）。"""

        return self.authority is not SignalAuthority.HINT and self.outcome in _REROUTABLE_OUTCOMES

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "outcome": self.outcome.value,
            "authority": self.authority.value,
            "reason": self.reason,
            "retryable": self.retryable,
        }
        if self.reset_at is not None:
            payload["reset_at"] = self.reset_at
        return payload

    @classmethod
    def from_dict(cls, payload: object) -> "ProviderFailureClassification | None":
        """讀回既有 job/manifest 上存的分類結果；格式不符一律回 None（fail-soft，
        不阻塞既有讀路徑——這只是輔助分類，不是授權欄位）。
        """

        if not isinstance(payload, Mapping):
            return None
        keys = set(payload)
        if not _PROVIDER_OUTCOME_FIELDS <= keys or keys - (
            _PROVIDER_OUTCOME_FIELDS | _PROVIDER_OUTCOME_OPTIONAL_FIELDS
        ):
            return None
        outcome = payload.get("outcome")
        authority = payload.get("authority")
        reason = payload.get("reason")
        try:
            outcome_enum = ProviderOutcome(outcome)
            authority_enum = SignalAuthority(authority)
        except ValueError:
            return None
        if not isinstance(reason, str) or not reason:
            return None
        reset_at = payload.get("reset_at")
        if reset_at is not None and (not isinstance(reset_at, int) or isinstance(reset_at, bool)):
            return None
        return cls(
            outcome=outcome_enum,
            authority=authority_enum,
            reason=reason,
            reset_at=reset_at,
        )


def _structured_classification(
    kind: outcome_taxonomy.StructuredKind,
    detail: str,
    *,
    reset_at: int | None = None,
) -> ProviderFailureClassification:
    return ProviderFailureClassification(
        _OUTCOME_BY_STRUCTURED_KIND[kind],
        SignalAuthority.STRUCTURED,
        detail,
        reset_at=reset_at,
    )


def _normalize_executable_token(value: str) -> str:
    token = value.strip().strip("`'\"")
    token = token.rsplit("/", 1)[-1]
    token = token.rsplit("\\", 1)[-1]
    return token.lower()


def _is_path_within_root(path_value: str, root: str | None) -> bool:
    if not root:
        return False
    candidate = Path(path_value)
    if not candidate.is_absolute():
        return False
    try:
        resolved_candidate = candidate.resolve(strict=False)
        resolved_root = Path(root).resolve(strict=False)
    except OSError:
        return False
    try:
        resolved_candidate.relative_to(resolved_root)
    except ValueError:
        return False
    return True


def _exception_summary(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _is_missing_executable_exception(
    exc: BaseException,
    *,
    executor: str | None,
    worktree: str | None,
) -> bool:
    if not isinstance(exc, FileNotFoundError):
        return False
    filename = exc.filename
    if not isinstance(filename, str) or not filename:
        return False
    if _is_path_within_root(filename, worktree):
        return False
    token = _normalize_executable_token(filename)
    if token in _SHARED_LAUNCH_INFRASTRUCTURE_TOKENS:
        return False
    if executor is not None and token == executor.lower():
        return True
    return token in _KNOWN_PROVIDER_EXECUTABLE_TOKENS


def classify_launch_failure(
    *,
    detail: str | None = None,
    exc: BaseException | None = None,
    executor: str | None = None,
    worktree: str | None = None,
) -> ProviderFailureClassification:
    """把 launch 階段（attach_launch_handle 之前）失敗投影成 structured outcome。"""

    if detail is None:
        if exc is None:
            raise ValueError("launch failure classification requires detail or exc")
        detail = f"launch failed before attach_launch_handle: {_exception_summary(exc)}"
    kind = outcome_taxonomy.StructuredKind.LAUNCH_FAILED
    if exc is not None and _is_missing_executable_exception(
        exc, executor=executor, worktree=worktree
    ):
        kind = outcome_taxonomy.StructuredKind.EXECUTABLE_NOT_FOUND
    return _structured_classification(kind, detail)


def classify_provider_failure(
    *,
    exit_code: int,
    output: str | None,
    now: object | None = None,
    tz=None,
) -> ProviderFailureClassification:
    """把一次 executor 失敗的 (exit_code, 合併 stdout/stderr 文字) 分類成 typed outcome。

    分兩層，順序不可互換（#499／#500／#487）：

    1. **結構化終局證據優先**（:func:`outcome_taxonomy.classify_structured_evidence`）
       ——provider 自己用機器可讀欄位講明白的事（`rate_limit_event.status =
       rejected`、終局 `api_error_status = 429`、`aborted_tools` 工具鏈中止、
       controller interruption）具
       ``STRUCTURED`` authority，不該被下一層的關鍵字比對翻案。#499 的 429 與
       #500 的 `aborted_streaming` 都在這一層定案。
    2. **文字關鍵字**（:func:`outcome_taxonomy.classify_text`）——只掃
       provider 層證據；nested tool result 與 init metadata 已在
       :func:`outcome_taxonomy.parse_stream_evidence` 被排除（#500／#487），
       模型散文只參與 content 判定。判定順序沿用 #369/#370（rate limit 先於
       auth）。

    呼叫端只在確認這是一次失敗（exit_code != 0 或呼叫端已知 status ==
    "failed"）時呼叫本函式；exit_code == 0 時仍會回傳一個防禦性的 UNKNOWN/HINT
    分類而不拋錯，避免呼叫端誤用時整條鏈路炸掉。
    """

    if exit_code == 0:
        return ProviderFailureClassification(
            ProviderOutcome.UNKNOWN,
            SignalAuthority.HINT,
            "exit code 0 -- classify_provider_failure 不應被呼叫在成功案例",
        )
    if exit_code == 127 and output is None:
        return ProviderFailureClassification(
            ProviderOutcome.UNKNOWN,
            SignalAuthority.HINT,
            "exit 127 without readable provider output is not enough to prove missing executable",
        )

    evidence = outcome_taxonomy.parse_stream_evidence(output)

    structured = outcome_taxonomy.classify_structured_evidence(evidence)
    if structured is not None:
        return _structured_classification(
            structured.kind,
            structured.detail,
            reset_at=structured.reset_at,
        )

    classification = outcome_taxonomy.classify_text(
        exit_code=exit_code,
        provider_text=evidence.provider_text,
        model_text=evidence.model_text,
        now=time.time() if now is None else now,
        tz=tz,
    )
    outcome = _OUTCOME_BY_TEXT_SIGNAL[classification.signal]
    authority = (
        SignalAuthority.HINT
        if classification.signal is outcome_taxonomy.TextSignal.NONE
        else SignalAuthority.TEXT_SIGNAL
    )
    return ProviderFailureClassification(
        outcome,
        authority,
        classification.detail,
        reset_at=classification.reset_at,
        reset_parser=classification.reset_parser,
    )


def classification_from_job(job: Mapping[str, object]) -> ProviderFailureClassification | None:
    """從 job registry row 讀回既有的分類結果（`Dispatcher._finalize_headless`
    在 finalize 當下寫入的 ``job["provider_outcome"]``）。找不到／格式不符一律
    回 None——呼叫端 fail-soft 退回既有無分類行為，不是 fail-closed 授權欄位。
    """

    return ProviderFailureClassification.from_dict(job.get("provider_outcome"))


def read_log_tail(log_path: str | None, *, max_bytes: int = 65536) -> str | None:
    """讀 headless job log 檔尾端至多 ``max_bytes`` bytes，供分類用。

    只讀尾端而非整檔：日誌可能很大（長時間 session），分類只關心最後出現的
    錯誤訊號；有界讀取避免熱路徑上的大檔 I/O 成本。壞檔/缺檔一律回 None
    （fail-soft——讀不到 log 不該讓 finalize 整條路徑炸掉，只是分類退化成
    UNKNOWN/HINT）。
    """

    if not log_path:
        return None
    try:
        with open(log_path, "rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            raw = handle.read()
    except OSError:
        return None
    return raw.decode("utf-8", errors="replace")
