"""#261：terminal/result contract 的 canonical envelope 與確定性採信規則。

本模組是「成功必須被證明」這條規則的單一真相源。設計依據見
``docs/superpowers/specs/terminal-result-contract-{spec,design}.md``：

- R1／D1：帶 ``schema_version`` 的 canonical envelope，三類 card 共用，且
  ``passed``／``failed``／``needs_human`` 三種終局狀態在契約上對等可達；舊形狀
  （``schema_version`` 非 canonical 值）走相容讀取路徑並帶可觀測 legacy 標記。
- R2／D2／D3：``passed`` 的採信條件是 manager 端能重讀並重驗 gate ledger，而不是
  card 宣稱跑過。矛盾偵測排在狀態採信之前，偵測到矛盾即 fail closed 並保留
  「哪一個 gate、期望值、實際值」。
- R3／D4／D5：StructuredOutput wrapper 只認白名單外層鍵，且同一個確定性 mismatch
  只嘗試修復一次；retry 有上限與計數器，計數與 validation path／reason 可被
  status surface 讀取。
- R4／D6：parse 失敗時保留唯讀診斷，但診斷欄位與授權欄位分離，可觀測不等於可授權。

本模組刻意維持純函式／純資料，不 import manager，也不碰 registry；呼叫端負責把
結果接到 lifecycle 上。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


# canonical envelope 的版本。舊形狀（1）不即刻失效，走 legacy 相容路徑。
TERMINAL_SCHEMA_VERSION = 2
LEGACY_SCHEMA_VERSIONS = (1,)

# 三種終局狀態在契約上對等可達；不得存在只有成功形狀合法的路徑。
TERMINAL_STATUSES = ("passed", "failed", "needs_human")

# 誠實表達失敗的兩種狀態；出現任一者都必須 fail closed 而不是被當成 schema 壞掉。
NON_PASSING_STATUSES = ("failed", "needs_human")

# 三類 card 共用同一 envelope。
TERMINAL_KINDS = (
    "workflow-card",
    "workflow-verification-result",
    "workflow-review-result",
)

# D4：只認明確列舉的外層包裝鍵，不做遞迴搜尋、不做寬鬆解析。
WRAPPER_KEYS = ("input", "params", "parameters", "arguments", "payload", "response")

# D4：同一個確定性 mismatch 只嘗試一次修復。
MAX_NORMALIZE_ATTEMPTS = 1

# D5：同一個確定性 mismatch 最多回派模型的次數上限。
MAX_SCHEMA_RETRIES = 2

GATE_LEDGER_SCHEMA_VERSION = 1
GATE_LEDGER_KIND = "workflow-gate-ledger"

# #307：red-required（tdd-red）卡語意反轉的唯一對象——manager 認可的「測試 gate」
# 名稱，對應 operator 慣例宣告的 ``PSC_GATE_CMD_PYTEST``
# （gate_ledger.GATE_ENV_PREFIX + "PYTEST" → 小寫 gate 名 "pytest"）。deck/data
# /cards.yaml 的 tdd-red 卡以 ``runtime_capabilities: ["module:pytest"]`` 明確
# 宣告依賴 pytest；其餘宣告的 gate（openspec／policy…）不受影響，仍走一般
# fail-closed 規則——語意反轉刻意只精準命中這一個 gate 名稱，不是放寬整張卡。
RED_REQUIRED_TEST_GATE_NAME = "pytest"

# pytest 的標準 exit code（見 ``_pytest.config.ExitCode``）。只有
# ``TESTS_FAILED``（1，代表「測試被收集、確實執行，且至少一個失敗」）是
# red-required 要求的合格 RED 證據。其餘：``OK``（0，全綠＝沒有產生 RED）、
# ``INTERRUPTED``（2，也是 collection/import error 的退出碼）、
# ``INTERNAL_ERROR``（3）、``USAGE_ERROR``（4）、``NO_TESTS_COLLECTED``（5）
# 都代表「builder 根本沒寫測試」或「測試檔壞掉」，一律維持 fail closed，不視為
# 合格 RED。
PYTEST_EXIT_TESTS_FAILED = 1

class TerminalContractError(ValueError):
    """確定性的 terminal contract 違規；一律 fail closed，且錯誤可被機器讀取。"""

    def __init__(
        self,
        message: str,
        *,
        reason: str,
        validation_path: str = "$",
        errors: tuple[dict[str, Any], ...] = (),
        attempts: int = 0,
        requires_model_retry: bool = False,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.validation_path = validation_path
        self.errors = errors
        self.attempts = attempts
        # 確定性錯誤重試模型不會改善結果，只會複製成本（D4），因此預設 False。
        self.requires_model_retry = requires_model_retry

    def as_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "validation_path": self.validation_path,
            "errors": [dict(item) for item in self.errors],
            "message": str(self),
        }

    def signature(self) -> str:
        """同一形狀的確定性 mismatch 具有相同 signature，供 retry 上限計數。"""

        return f"{self.validation_path}|{self.reason}"


class GateContradictionError(TerminalContractError):
    """terminal 自稱的狀態與 manager 重讀到的 gate 結果矛盾（R2／D3）。"""

    def __init__(self, *, gate: str, expected: str, actual: str, detail: str = "") -> None:
        message = (
            f"terminal 自稱 {expected} 但 gate {gate!r} 實際為 {actual}"
            + (f"：{detail}" if detail else "")
        )
        super().__init__(
            message,
            reason="gate-status-contradiction",
            validation_path="$.gate_evidence",
            errors=(
                {
                    "gate": gate,
                    "expected": expected,
                    "actual": actual,
                    "detail": detail,
                },
            ),
        )
        self.gate = gate
        self.expected = expected
        self.actual = actual


@dataclass(frozen=True)
class NormalizedPayload:
    """一次（且僅一次）白名單 wrapper 正規化的結果。"""

    payload: dict[str, Any]
    unwrapped_key: str | None
    attempts: int

    @property
    def requires_model_retry(self) -> bool:
        """已成功取得 canonical payload → 不回派模型（D4）。"""

        return False


@dataclass(frozen=True)
class TerminalEnvelope:
    """canonical terminal/result envelope 的解析結果。"""

    schema_version: int
    kind: str
    status: str
    payload: dict[str, Any]
    diagnostics: dict[str, Any]
    gate_evidence: tuple[dict[str, Any], ...]
    legacy: bool
    run_id: str | None
    card_id: str | None


@dataclass(frozen=True)
class TerminalAuthorization:
    """狀態採信的結果。``authorized`` 只在 passed 且 gate evidence 重驗通過時為 True。"""

    status: str
    authorized: bool
    verified_gates: tuple[str, ...]
    ledger_digest: str | None
    legacy: bool


@dataclass(frozen=True)
class TerminalDiagnostics:
    """parse 失敗時保留的唯讀診斷（R4／D6）。

    刻意不含任何授權語意的欄位；``observed_head`` 是「觀察到的」而非「已授權的」，
    因此 :meth:`candidate_authority` 永遠回 ``None``。
    """

    job_id: str
    observed_head: str | None
    reason: str
    validation_path: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": "terminal-parse-diagnostics",
            "job_id": self.job_id,
            "observed_head": self.observed_head,
            "reason": self.reason,
            "validation_path": self.validation_path,
            "authority_granted": False,
        }

    def candidate_authority(self) -> None:
        """診斷資訊永遠不構成 candidate authority。"""

        return None


@dataclass
class SchemaRetryLedger:
    """同一確定性 mismatch 的 retry 計數與上限（R3／D5）。"""

    limit: int = MAX_SCHEMA_RETRIES
    counts: dict[str, int] = field(default_factory=dict)
    total: int = 0
    last_validation_path: str | None = None
    last_validation_reason: str | None = None

    def record(self, signature: str, *, validation_path: str, reason: str) -> int:
        """記一次確定性 mismatch，回傳夾在上限內的計數。"""

        current = min(self.counts.get(signature, 0) + 1, self.limit)
        self.counts[signature] = current
        self.total += 1
        self.last_validation_path = validation_path
        self.last_validation_reason = reason
        return current

    def exhausted(self, signature: str) -> bool:
        return self.counts.get(signature, 0) >= self.limit

    def status_fields(self) -> dict[str, Any]:
        """status／inspect 可觀察的欄位（D5）。"""

        return {
            # 已計入上限的實際 retry 次數（每個 signature 各自被 limit 夾住），
            # 因此這個數字不會隨著同一個確定性 mismatch 反覆出現而無限成長。
            "schema_retry_count": sum(self.counts.values()),
            # 觀察到的 mismatch 總次數（含超過上限後被拒的），供成本診斷。
            "schema_mismatch_observed": self.total,
            "schema_retry_limit": self.limit,
            "last_validation_path": self.last_validation_path,
            "last_validation_reason": self.last_validation_reason,
            "schema_retry_exhausted": any(
                count >= self.limit for count in self.counts.values()
            ),
        }


def _is_canonical_shape(value: object) -> bool:
    """僅做形狀辨識（是否為 terminal payload），不做合法性判定。"""

    return (
        isinstance(value, Mapping)
        and type(value.get("schema_version")) is int
        and value.get("kind") in TERMINAL_KINDS
    )


def normalize_structured_output(raw: object) -> NormalizedPayload:
    """把 StructuredOutput 回傳值收斂成 canonical payload。

    只處理白名單的單層外層包裝鍵，且只嘗試一次；未知形狀終止為可操作錯誤，
    不以寬鬆解析（遞迴尋找看起來像 canonical 的 dict）吞掉未知欄位。
    """

    if not isinstance(raw, Mapping):
        raise TerminalContractError(
            f"StructuredOutput payload 不是物件：{type(raw).__name__}",
            reason="payload-not-object",
            validation_path="$",
            errors=({"observed_type": type(raw).__name__},),
        )
    if _is_canonical_shape(raw):
        return NormalizedPayload(payload=dict(raw), unwrapped_key=None, attempts=0)

    observed_keys = sorted(str(key) for key in raw)
    if len(raw) == 1:
        only_key = next(iter(raw))
        if only_key in WRAPPER_KEYS:
            inner = raw[only_key]
            # 只剝一層：巢狀包裝屬於同一個確定性 mismatch，不再嘗試第二次。
            if _is_canonical_shape(inner):
                return NormalizedPayload(
                    payload=dict(inner),
                    unwrapped_key=str(only_key),
                    attempts=MAX_NORMALIZE_ATTEMPTS,
                )
            raise TerminalContractError(
                f"白名單 wrapper {only_key!r} 內層不是 canonical terminal payload；"
                f"已用盡 {MAX_NORMALIZE_ATTEMPTS} 次正規化嘗試",
                reason="wrapper-shape-unrecognized",
                validation_path=f"$.{only_key}",
                errors=(
                    {
                        "observed_keys": sorted(str(key) for key in inner)
                        if isinstance(inner, Mapping)
                        else [],
                        "allowed_wrapper_keys": list(WRAPPER_KEYS),
                        "expected_kinds": list(TERMINAL_KINDS),
                    },
                ),
                attempts=MAX_NORMALIZE_ATTEMPTS,
            )

    raise TerminalContractError(
        "StructuredOutput 形狀不在 wrapper 白名單內，且不得以寬鬆解析取用內層欄位；"
        f"觀察到的外層鍵={observed_keys}，白名單={list(WRAPPER_KEYS)}",
        reason="wrapper-shape-unrecognized",
        validation_path="$",
        errors=(
            {
                "observed_keys": observed_keys,
                "allowed_wrapper_keys": list(WRAPPER_KEYS),
                "expected_kinds": list(TERMINAL_KINDS),
            },
        ),
        attempts=MAX_NORMALIZE_ATTEMPTS,
    )


def validate_envelope(payload: object) -> TerminalEnvelope:
    """驗證 canonical envelope；舊 schema_version 走 legacy 相容路徑。"""

    if not isinstance(payload, Mapping):
        raise TerminalContractError(
            f"terminal payload 不是物件：{type(payload).__name__}",
            reason="payload-not-object",
        )
    schema_version = payload.get("schema_version")
    if type(schema_version) is not int:
        raise TerminalContractError(
            "terminal payload 缺少整數 schema_version",
            reason="schema-version-missing",
            validation_path="$.schema_version",
        )
    kind = payload.get("kind")
    if kind not in TERMINAL_KINDS:
        raise TerminalContractError(
            f"terminal payload kind 非法：{kind!r}",
            reason="kind-unknown",
            validation_path="$.kind",
            errors=({"observed": kind, "expected": list(TERMINAL_KINDS)},),
        )
    legacy = schema_version != TERMINAL_SCHEMA_VERSION
    if legacy and schema_version not in LEGACY_SCHEMA_VERSIONS:
        raise TerminalContractError(
            f"terminal payload schema_version 不受支援：{schema_version}",
            reason="schema-version-unsupported",
            validation_path="$.schema_version",
            errors=(
                {
                    "observed": schema_version,
                    "canonical": TERMINAL_SCHEMA_VERSION,
                    "legacy": list(LEGACY_SCHEMA_VERSIONS),
                },
            ),
        )

    status = payload.get("status")
    if legacy and status is None:
        # 舊 review 形狀不帶 status；相容讀取路徑不因此拒收既有 run。
        status = "passed"
    if legacy and status == "verified":
        # 舊 verification 形狀用 "verified" 表達成功；映射成 canonical 的 passed，
        # 讓矛盾偵測對三類 card 一致生效（canonical envelope 不接受這個值）。
        status = "passed"
    if status not in TERMINAL_STATUSES:
        raise TerminalContractError(
            f"terminal status 不是終局狀態：{status!r}；"
            f"合法值為 {list(TERMINAL_STATUSES)}",
            reason="status-not-terminal",
            validation_path="$.status",
            errors=({"observed": status, "expected": list(TERMINAL_STATUSES)},),
        )

    diagnostics = payload.get("diagnostics", {})
    if not isinstance(diagnostics, Mapping):
        raise TerminalContractError(
            "terminal diagnostics 必須為物件",
            reason="diagnostics-invalid",
            validation_path="$.diagnostics",
        )
    gate_evidence_raw = payload.get("gate_evidence", [])
    if not isinstance(gate_evidence_raw, list) or any(
        not isinstance(item, Mapping) for item in gate_evidence_raw
    ):
        raise TerminalContractError(
            "terminal gate_evidence 必須為物件陣列",
            reason="gate-evidence-invalid",
            validation_path="$.gate_evidence",
        )
    gate_evidence: list[dict[str, Any]] = []
    for index, item in enumerate(gate_evidence_raw):
        name = item.get("name")
        sha256 = item.get("sha256")
        claimed = item.get("status")
        if not isinstance(name, str) or not name:
            raise TerminalContractError(
                "gate_evidence 項目缺少 name",
                reason="gate-evidence-invalid",
                validation_path=f"$.gate_evidence[{index}].name",
            )
        # sha256 為選填的 provenance 欄位：ledger 是在模型結束之後才由 manager 產生的，
        # 模型不可能知道它的 digest，因此不得把 digest 設為模型的義務。
        if sha256 is not None and (not isinstance(sha256, str) or len(sha256) != 64):
            raise TerminalContractError(
                f"gate_evidence {name!r} 的 sha256 格式非法",
                reason="gate-evidence-invalid",
                validation_path=f"$.gate_evidence[{index}].sha256",
            )
        if claimed not in {"passed", "failed"}:
            raise TerminalContractError(
                f"gate_evidence {name!r} status 非法：{claimed!r}",
                reason="gate-evidence-invalid",
                validation_path=f"$.gate_evidence[{index}].status",
            )
        gate_evidence.append({"name": name, "status": claimed, "sha256": sha256})

    return TerminalEnvelope(
        schema_version=schema_version,
        kind=str(kind),
        status=str(status),
        payload=dict(payload),
        diagnostics=dict(diagnostics),
        gate_evidence=tuple(gate_evidence),
        legacy=legacy,
        run_id=payload.get("run_id") if isinstance(payload.get("run_id"), str) else None,
        card_id=payload.get("card_id") if isinstance(payload.get("card_id"), str) else None,
    )


def gate_ledger_path(log_path: str | Path) -> Path:
    """由 job 的 ``log_path`` 推導 gate ledger 路徑（``<...>.jsonl`` → ``<...>.gates.json``）。

    刻意與 :func:`paulsha_cortex.coordinator.dispatcher.exit_sentinel_path` 同一套
    推導方式：ledger 由 manager 掌控的 wrapper script 寫在 manager 自己的 log_dir，
    模型的 cwd 是 worktree、也拿不到這個路徑，因此 ledger 不是模型能產生或改寫的。
    """

    path = Path(log_path)
    return path.with_name(path.stem + ".gates.json")


def gate_ledger_digest(payload: Mapping[str, Any]) -> str:
    """gate ledger 的 canonical digest；重驗以「讀檔＋比 hash」為主（D2）。"""

    encoded = json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def read_gate_ledger(ledger_path: str | Path) -> tuple[dict[str, Any], str] | None:
    """讀回 manager 側 gate ledger 與其 digest；不存在回 ``None``。"""

    path = Path(ledger_path)
    if path.is_symlink():
        raise TerminalContractError(
            "gate ledger 不得為 symlink",
            reason="gate-ledger-unsafe",
            validation_path="$.gate_evidence",
        )
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TerminalContractError(
            f"gate ledger 無法讀取：{exc}",
            reason="gate-ledger-unreadable",
            validation_path="$.gate_evidence",
        ) from exc
    if (
        not isinstance(payload, Mapping)
        or payload.get("kind") != GATE_LEDGER_KIND
        or type(payload.get("schema_version")) is not int
        or not isinstance(payload.get("gates"), list)
    ):
        raise TerminalContractError(
            "gate ledger 形狀非法",
            reason="gate-ledger-invalid",
            validation_path="$.gate_evidence",
        )
    return dict(payload), gate_ledger_digest(payload)


def _ledger_outcomes(payload: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """把 ledger 收斂成 gate → 實際結果；exit_code 非 0 一律視為 failed。

    刻意讓 exit_code 覆寫自述的 status：ledger 自身矛盾（記了非 0 exit code 卻標
    passed）與「terminal 說謊」是同一類問題，都不得被採信。
    """

    outcomes: dict[str, dict[str, Any]] = {}
    for entry in payload.get("gates", []):
        if not isinstance(entry, Mapping):
            raise TerminalContractError(
                "gate ledger gates 項目必須為物件",
                reason="gate-ledger-invalid",
                validation_path="$.gate_evidence",
            )
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise TerminalContractError(
                "gate ledger gate 缺少 name",
                reason="gate-ledger-invalid",
                validation_path="$.gate_evidence",
            )
        exit_code = entry.get("exit_code")
        declared = entry.get("status")
        actual = "passed" if declared == "passed" else "failed"
        if type(exit_code) is int and exit_code != 0:
            actual = "failed"
        detail = entry.get("detail") if isinstance(entry.get("detail"), str) else ""
        if actual == "failed" and not detail and type(exit_code) is int:
            detail = f"exit_code={exit_code}"
        outcomes[name] = {"status": actual, "detail": detail, "exit_code": exit_code}
    return outcomes


def _apply_red_required_semantics(
    outcomes: Mapping[str, dict[str, Any]], *, test_policy: str | None
) -> dict[str, dict[str, Any]]:
    """#307：red-required 卡對「測試 gate」語意反轉——RED（測試失敗）才是達成。

    只精準命中 :data:`RED_REQUIRED_TEST_GATE_NAME` 這一個 ledger 項；其餘 gate
    （openspec／policy…）原樣返回，一般卡（``test_policy`` 非 ``red-required``）
    整份 outcomes 原樣返回，不改動既有 fail-closed 行為。

    只有 pytest exit code 精確等於 :data:`PYTEST_EXIT_TESTS_FAILED`（1）才視為
    合格 RED 證據並反轉為 ``passed``；exit code 0（全綠，未產生 RED）反轉為
    ``failed``——red-required 的必要條件是測試確實失敗，全綠代表沒有交付要求的
    RED regression test。其餘 exit code（2/3/4/5…，collection error／
    interrupted／internal error／usage error／no tests collected）或缺少
    ``exit_code`` 一律維持既有的 ``failed`` 判定，不做任何轉換——這些狀況代表
    「builder 根本沒寫測試」或「測試檔壞掉」，不是合格的 RED，必須繼續 fail
    closed。
    """

    if test_policy != "red-required":
        return dict(outcomes)
    entry = outcomes.get(RED_REQUIRED_TEST_GATE_NAME)
    if entry is None:
        return dict(outcomes)
    exit_code = entry.get("exit_code")
    transformed = dict(outcomes)
    if type(exit_code) is int and exit_code == PYTEST_EXIT_TESTS_FAILED:
        transformed[RED_REQUIRED_TEST_GATE_NAME] = {
            **entry,
            "status": "passed",
            "detail": (
                f"red-required：pytest exit_code={exit_code}（測試如預期失敗，"
                f"視為達成 RED 要求）；原始 detail={entry.get('detail') or ''!r}"
            ),
        }
    elif entry.get("status") == "passed":
        transformed[RED_REQUIRED_TEST_GATE_NAME] = {
            **entry,
            "status": "failed",
            "detail": (
                "red-required：pytest exit_code=0（全數通過，未產生 RED 失敗）；"
                "不符合 red-required 要求"
            ),
        }
    return transformed


def authorize_terminal(
    envelope: TerminalEnvelope,
    *,
    ledger_path: str | Path,
    require_ledger: bool = False,
    test_policy: str | None = None,
) -> TerminalAuthorization:
    """採信 terminal 狀態；``passed`` 必須由 manager 獨立產生的 gate ledger 授權（R2）。

    ledger 由 :mod:`paulsha_cortex.coordinator.gate_ledger` 在模型行程結束**之後**、
    於 manager 掌控的 wrapper script 內產生，因此它不是模型講的話。envelope 內的
    ``gate_evidence`` 只是模型的自述宣告，這裡用 ledger 去對照那份宣告。

    D3：矛盾偵測優先於狀態採信——先做確定性 cross-check，矛盾即 fail closed，
    才進入正常的狀態處理，避免「先按 passed 走一段流程」造成部分副作用。

    #307：``test_policy="red-required"`` 時，:func:`_apply_red_required_semantics`
    只對 :data:`RED_REQUIRED_TEST_GATE_NAME` 這一項 ledger 結果做語意反轉，且只
    影響「terminal 自稱 passed 是否與 ledger 矛盾」這條判斷；模型自述的
    ``gate_evidence`` 仍對照未反轉的原始 ledger 事實，維持誠實性檢查不被稀釋。
    其他 ``test_policy``（含 ``None``）不受影響，一般卡的 fail-closed 行為不變。
    """

    if envelope.status != "passed":
        # 失敗與需人工介入不需要 gate evidence，才不會逼模型只能回成功形狀（R1）。
        return TerminalAuthorization(
            status=envelope.status,
            authorized=False,
            verified_gates=(),
            ledger_digest=None,
            legacy=envelope.legacy,
        )

    found = read_gate_ledger(ledger_path)
    ledger = found[0] if found is not None else None
    digest = found[1] if found is not None else None

    if ledger is None:
        if require_ledger:
            # ledger 不存在 = manager 掌控的 wrapper 沒跑完 gate 階段。此時沒有任何
            # 獨立證據可以背書 passed；模型文字、exit code 為 0、無明確錯誤三者
            # 皆不構成成功授權，故 fail closed。
            raise TerminalContractError(
                "terminal 宣稱 passed 但 manager 端沒有可重驗的 gate ledger"
                f"（{Path(ledger_path).name}）；模型文字、exit code 為 0、"
                "無明確錯誤皆不構成成功授權",
                reason="gate-evidence-missing",
                validation_path="$.gate_evidence",
                errors=({"ledger_path": str(ledger_path)},),
            )
        # 未要求 ledger 的 phase（例如不跑 gate 的 plan card）維持既有行為。
        return TerminalAuthorization(
            status="passed",
            authorized=True,
            verified_gates=(),
            ledger_digest=None,
            legacy=envelope.legacy,
        )

    outcomes = _ledger_outcomes(ledger)
    # #307：red-required 卡的語意反轉只作用在這裡（矛盾偵測），不影響下面的
    # gate_evidence 誠實性 cross-check——那裡刻意繼續用未反轉的 `outcomes`。
    effective_outcomes = _apply_red_required_semantics(outcomes, test_policy=test_policy)
    # D3：先做矛盾偵測。即使 terminal 沒有引用該 gate，manager 讀到的失敗結果
    # 也直接否決 passed——「沒提到」不能當作「沒失敗」。
    for name in sorted(effective_outcomes):
        actual = effective_outcomes[name]
        if actual["status"] != "passed":
            raise GateContradictionError(
                gate=name,
                expected="passed",
                actual=actual["status"],
                detail=str(actual.get("detail") or ""),
            )

    # 模型自述的 gate 宣告必須與 ledger 一致：宣稱跑了 ledger 沒有的 gate，或宣稱
    # 的結果與 ledger 不符，都是 R2 定義的矛盾（前者代表自述不可信，後者已被上面
    # 的迴圈攔下，這裡補上「宣稱 failed 卻整體 passed」這類不一致）。
    #
    # #308：operator 顯式零 gate（ledger 存在但 gates 為空）時跳過此對照——此設定
    # 依 #261 文件本就沒有 R2 保護，空 ledger 下沒有可對照的獨立證據層；模型自述
    # 不構成授權，對照它只會讓授權結果隨模型是否填寫 gate_evidence 而隨機化
    # （gpt-5.4 會把 shell 指令如 `pwd` 填進 gate_evidence）。ledger 非空時維持
    # fail-closed。
    if not outcomes:
        return TerminalAuthorization(
            status="passed",
            authorized=True,
            verified_gates=(),
            ledger_digest=digest,
            legacy=envelope.legacy,
        )
    for item in envelope.gate_evidence:
        name = item["name"]
        if name not in outcomes:
            raise TerminalContractError(
                f"terminal 宣稱跑了 gate {name!r}，但 manager 的 ledger 沒有這一項",
                reason="gate-evidence-unknown-gate",
                validation_path="$.gate_evidence",
                errors=({"gate": name, "known": sorted(outcomes)},),
            )
        if item["status"] != outcomes[name]["status"]:
            raise GateContradictionError(
                gate=name,
                expected=str(item["status"]),
                actual=str(outcomes[name]["status"]),
                detail=str(outcomes[name].get("detail") or ""),
            )

    return TerminalAuthorization(
        status="passed",
        authorized=True,
        verified_gates=tuple(sorted(outcomes)),
        ledger_digest=digest,
        legacy=envelope.legacy,
    )
