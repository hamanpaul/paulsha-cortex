"""診斷 invariant 家族（#527／#514／#515／#511／#482）的單一理由契約。

0813–0814 五次獨立命中同一條缺口：狀態被推向「人要接手」的那一刻，**理由沒有
跟著落地**。五個現場形態不同、根卻同一條——

- **#527**：`manager_daemon` 的 workflow resume 迴圈把例外只 `print` 進
  `~/.agents/log/manager.log`，run 上只留一個沒有理由的 `needs_human` facet；
  `cortex status`／`work show`／`tick`／`complete` 四個介面同時沉默。
- **#514**：`_validated_brainstorm_planning_authority()` 對已發佈 artifact 的
  重驗失敗只丟一句 `workflow brainstorm artifact is not accepted`，不含 ref、
  不含 `assess_planning_artifact()` 已經算好的 `reasons`／`markers`。
- **#515**：`_post_integration_artifact_evidence()` 的 14 個裸 `return None`
  把 symlink／路徑逃逸／解碼失敗／assessment 拒收全塌縮成一個
  `primary-artifact-invalid`——環境類與內容類無從分辨。
- **#511**：planning artifact 拒收未帶原因也不留存內容（PR #513 已修
  `_publish_planning_artifacts` 的訊息與 evidence，但那份結構化理由到不了 run，
  只能靠上游 `str(exc)[:160]` 截斷後的字串殘骸）。
- **#482**：retry-review 的 absent evidence key 不含原因也不含 identity，合法
  重試撞上 immutable artifact。

逐案補洞已證明無效（五次），因此把「理由」本身升格成**型別**：任何把 run 轉入
`needs_human`／`degraded`、或把 evidence 標為 absent 的狀態變更，都必須提供一份
:class:`DiagnosticReason`——機器可讀 ``reason`` ＋ 人可讀 ``detail`` ＋ 來源位置
``source``（＋可選的 evidence 位置與 context）。強制點在 registry 的狀態轉移
API（見 ``registry._manager_update_workflow_run``／``_manager_create_workflow_run``），
「忘記帶理由」因此在單元測試就炸，而不是在 dogfooding 現場靜默停滯。

**範圍**：本模組只管「診斷與理由」。後續處置（retry／needs_human／fail-closed
邏輯）一律不變——每個呼叫端原本會做什麼，改完之後照樣做什麼，只是多落一份可
稽核的理由。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

__all__ = [
    "DIAGNOSTIC_REASON_SCHEMA_VERSION",
    "DIAGNOSTIC_DETAIL_MAX_LENGTH",
    "DIAGNOSTIC_CONTEXT_VALUE_MAX_LENGTH",
    "DiagnosticInvariantError",
    "DiagnosticReason",
    "diagnostic_reason",
    "coerce_diagnostic_reason",
    "summarize_exception",
]


DIAGNOSTIC_REASON_SCHEMA_VERSION = 1

# `reason` 是機器可讀的分類碼：小寫、無空白、無換行。呼叫端既有的 reason 字面值
# （`planning-publication-drift`、`provider-retry-exhausted`、
# `reviewer-identity-missing` …）本來就是這個形狀，刻意沿用而不另立詞彙表——
# 收斂成封閉 enum 會逼所有 lane 同步一張表，正是 #542 想避免的第三處列舉。
# 底線一併放行：既有詞彙有一部分直接來自 snake_case 識別字（plan review 的
# `contract_compatibility`、provider outcome 的 `rate_limited`），把它們硬改成
# kebab 會讓同一個概念在 reason 與回傳值裡長得不一樣。
DIAGNOSTIC_REASON_CODE_RE = re.compile(r"[a-z0-9]+(?:[-_][a-z0-9]+)*")

# `source` 是來源位置：`<module>.<function>` 或再加 `:<標記>` 細分同一函式內的
# 多個出口（例如 `manager.resume_workflow_run:planning-authority`）。
DIAGNOSTIC_SOURCE_RE = re.compile(r"[A-Za-z0-9_.]+(?::[A-Za-z0-9_.\-]+)?")

# `detail` 上限：比照 `manager.PLANNING_ARTIFACT_REJECTION_MESSAGE_MAX_LENGTH`
# 的 400。完整內容一律以 evidence 為準，detail 只負責讓 operator 在 `cortex
# status` 一行內看懂「卡在什麼事上」。
DIAGNOSTIC_DETAIL_MAX_LENGTH = 400

# context 每個值的上限：context 是給機器再消費的小欄位（run_id／card／path／
# identity），不是給長文用的。
DIAGNOSTIC_CONTEXT_VALUE_MAX_LENGTH = 200

# context 最多幾個 key：擋住把整包 payload 倒進 run row 的用法。
DIAGNOSTIC_CONTEXT_MAX_KEYS = 16

_CONTEXT_KEY_RE = re.compile(r"[a-z0-9]+(?:_[a-z0-9]+)*")


class DiagnosticInvariantError(ValueError):
    """狀態轉入 needs_human／degraded／absent 卻沒帶結構化理由。

    刻意繼承 ``ValueError``：registry 全部的 fail-closed 驗證都是 ``ValueError``，
    而 daemon 的 tick isolation（#246）攔的也是 ``(ValueError, RuntimeError,
    OSError)``——沿用同一族，違反 invariant 不會把整個 daemon 打掛，但在單元
    測試裡會直接讓該筆狀態轉換失敗。
    """


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _single_line(text: str) -> str:
    """壓成單行。

    理由字串會流進 `recover-planning` 的 `failure_reason` 與 evidence 的
    `reason` 欄位，兩者都明確拒收換行（見 `work_actions`／`control.contract`
    對 `"\\n" in failure_reason` 的檢查）。
    """

    return " ".join(str(text).split())


@dataclass(frozen=True)
class DiagnosticReason:
    """一份結構化理由。

    三個必要欄位對應 invariant 的三個要素：

    - ``reason``：機器可讀分類碼（kebab-case），供 `work show`／`status` 分支與
      operator grep。
    - ``detail``：人可讀單行敘述，說明「實際發生什麼」。
    - ``source``：來源位置（`<module>.<function>[:<出口標記>]`），讓 operator
      不必反推是哪一條路徑寫的。

    ``evidence_refs`` 指向已落檔的完整內容（planning-artifacts rejection、
    planning-recovery、gate evaluation…）；``context`` 是小型 string→string 的
    機器可讀附註（run_id、card、path、reviewer identity…）。
    """

    reason: str
    detail: str
    source: str
    evidence_refs: tuple[str, ...] = ()
    context: Mapping[str, str] = field(default_factory=dict)
    # 可直接供 operator 執行的下一步；與 reason 一起持久化，避免呈現面重算後
    # 與當時實際寫入 needs_human 的處置提示漂移。
    next_step_hint: str | None = None
    recorded_at: str | None = None
    schema_version: int = DIAGNOSTIC_REASON_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != DIAGNOSTIC_REASON_SCHEMA_VERSION:
            raise DiagnosticInvariantError(
                f"diagnostic reason schema_version 非法: {self.schema_version!r}"
            )
        if not isinstance(self.reason, str) or DIAGNOSTIC_REASON_CODE_RE.fullmatch(self.reason) is None:
            raise DiagnosticInvariantError(
                f"diagnostic reason 必須為 kebab-case 機器可讀碼: {self.reason!r}"
            )
        if not isinstance(self.detail, str) or not self.detail.strip():
            raise DiagnosticInvariantError("diagnostic detail 必須為非空人可讀字串")
        if not isinstance(self.source, str) or DIAGNOSTIC_SOURCE_RE.fullmatch(self.source) is None:
            raise DiagnosticInvariantError(
                f"diagnostic source 必須為 <module>.<function>[:<標記>]: {self.source!r}"
            )
        detail = _single_line(self.detail)
        if len(detail) > DIAGNOSTIC_DETAIL_MAX_LENGTH:
            detail = detail[:DIAGNOSTIC_DETAIL_MAX_LENGTH].rstrip() + "…"
        object.__setattr__(self, "detail", detail)
        if not isinstance(self.evidence_refs, tuple) or any(
            not isinstance(item, str) or not item.strip() for item in self.evidence_refs
        ):
            raise DiagnosticInvariantError("diagnostic evidence_refs 必須為非空字串 tuple")
        if not isinstance(self.context, Mapping):
            raise DiagnosticInvariantError("diagnostic context 必須為 mapping")
        if len(self.context) > DIAGNOSTIC_CONTEXT_MAX_KEYS:
            raise DiagnosticInvariantError(
                f"diagnostic context key 數逾限（上限 {DIAGNOSTIC_CONTEXT_MAX_KEYS}）"
            )
        normalized: dict[str, str] = {}
        for key, value in self.context.items():
            if not isinstance(key, str) or _CONTEXT_KEY_RE.fullmatch(key) is None:
                raise DiagnosticInvariantError(f"diagnostic context key 非法: {key!r}")
            if value is None:
                continue
            text = _single_line(value)
            if len(text) > DIAGNOSTIC_CONTEXT_VALUE_MAX_LENGTH:
                text = text[:DIAGNOSTIC_CONTEXT_VALUE_MAX_LENGTH].rstrip() + "…"
            normalized[key] = text
        object.__setattr__(self, "context", normalized)
        if self.next_step_hint is not None:
            if not isinstance(self.next_step_hint, str) or not self.next_step_hint.strip():
                raise DiagnosticInvariantError(
                    "diagnostic next_step_hint 必須為非空人可讀字串"
                )
            object.__setattr__(self, "next_step_hint", _single_line(self.next_step_hint))
        if self.recorded_at is None:
            object.__setattr__(self, "recorded_at", _utcnow())
        elif not isinstance(self.recorded_at, str) or not self.recorded_at:
            raise DiagnosticInvariantError("diagnostic recorded_at 必須為 ISO8601 字串")

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "reason": self.reason,
            "detail": self.detail,
            "source": self.source,
            "recorded_at": self.recorded_at,
        }
        if self.evidence_refs:
            payload["evidence_refs"] = list(self.evidence_refs)
        if self.context:
            payload["context"] = dict(self.context)
        if self.next_step_hint is not None:
            payload["next_step_hint"] = self.next_step_hint
        return payload

    @classmethod
    def from_dict(cls, payload: object) -> DiagnosticReason:
        if not isinstance(payload, Mapping):
            raise DiagnosticInvariantError("diagnostic reason 格式錯誤")
        unknown = set(payload) - {
            "schema_version",
            "reason",
            "detail",
            "source",
            "recorded_at",
            "evidence_refs",
            "context",
            "next_step_hint",
        }
        if unknown:
            raise DiagnosticInvariantError(
                f"diagnostic reason 含未知欄位: {sorted(unknown)}"
            )
        evidence_refs = payload.get("evidence_refs", ())
        if isinstance(evidence_refs, (list, tuple)):
            evidence_refs = tuple(evidence_refs)
        else:
            raise DiagnosticInvariantError("diagnostic evidence_refs 格式錯誤")
        context = payload.get("context") or {}
        return cls(
            reason=payload.get("reason"),
            detail=payload.get("detail"),
            source=payload.get("source"),
            evidence_refs=evidence_refs,
            context=context,
            next_step_hint=payload.get("next_step_hint"),
            recorded_at=payload.get("recorded_at"),
            schema_version=payload.get("schema_version", DIAGNOSTIC_REASON_SCHEMA_VERSION),
        )

    def rendered(self) -> str:
        """單行摘要：`<reason>: <detail> (source=…; evidence=…)`。

        `cortex status` 的 attention 條目與例外訊息共用這一份渲染，避免同一份
        理由在兩個介面長得不一樣。
        """

        details = [f"source={self.source}"]
        if self.evidence_refs:
            details.append("evidence=" + ",".join(self.evidence_refs))
        return _single_line(f"{self.reason}: {self.detail} ({'; '.join(details)})")


def diagnostic_reason(
    reason: str,
    detail: str,
    *,
    source: str,
    evidence_refs: tuple[str, ...] | list[str] = (),
    next_step_hint: str | None = None,
    recorded_at: str | None = None,
    **context: object,
) -> DiagnosticReason:
    """建構 :class:`DiagnosticReason` 的呼叫端捷徑。

    ``**context`` 收 string→string 的機器可讀附註；``None`` 值自動略去，讓呼叫
    端可以直接把可能為 ``None`` 的欄位（`card`、`job_id`、`candidate`）傳進來
    而不必逐個判斷。
    """

    return DiagnosticReason(
        reason=reason,
        detail=detail,
        source=source,
        evidence_refs=tuple(evidence_refs),
        context={key: str(value) for key, value in context.items() if value is not None},
        next_step_hint=next_step_hint,
        recorded_at=recorded_at,
    )


def coerce_diagnostic_reason(value: object) -> DiagnosticReason | None:
    """把 registry 呼叫端傳進來的值正規化成 :class:`DiagnosticReason`。

    接受 ``DiagnosticReason`` 本身或它的 dict 投影（狀態檔往返後就是 dict），
    ``None`` 原樣回傳，其餘一律 fail-closed。
    """

    if value is None:
        return None
    if isinstance(value, DiagnosticReason):
        return value
    return DiagnosticReason.from_dict(value)


def summarize_exception(exc: BaseException, *, limit: int = 200) -> str:
    """例外的單行摘要：`<型別>: <訊息前 N 字>`。

    沿用 #397／#408 已定案的格式（`f"{type(exc).__name__}: {str(exc)[:160]}"`），
    只是收成一份實作——五個現場各自手寫一次正是理由格式漂移的來源。
    """

    return _single_line(f"{type(exc).__name__}: {str(exc)[:limit]}")
