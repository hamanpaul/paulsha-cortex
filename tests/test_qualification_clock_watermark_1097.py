"""#1097：qualification clock watermark 補上其他 durable 系統時間證據。

#842 的 ``clock_watermarks`` 只在「某次 qualification 查詢或寫入」觀測到
``now >= expires_at`` 之後才會落盤，防止之後較早的時鐘重新把已過期資格
救活。但若資格已在真實時間過期、**第一次 qualification 查詢發生在系統時鐘
回撥之後**（回撥前完全沒有任何 qualification 觀測落盤），watermark 本身
無從得知真實時間已越過 ``expires_at``。

本檔驗證 :func:`qualification_lifecycle._clock_evidence_floor` 與
``_qualification_status_unlocked`` 如何用其他 Manager-only durable 落盤狀態
（``jobs.json`` 與 ``quota-admission-decisions``）補上這個下界：

- 回撥情境（其他證據晚於 ``expires_at``）→ fail-closed。
- 證據不可讀／缺席 → 維持 #1097 之前的行為，並帶出可機讀的
  ``clock-evidence-unavailable`` 診斷。
- 正常時間前進、證據存在但早於 ``expires_at`` → 不受影響。

全程以字串／檔案 mtime 明確指定時間，不依賴真實時間（也不呼叫
``datetime.now()``）。
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path

import pytest

from paulsha_cortex.config import paths as config_paths
from paulsha_cortex.coordinator import qualification_lifecycle as lifecycle
from paulsha_cortex.coordinator.qualification_lifecycle import QualificationStore


NOW = "2026-09-26T00:00:01Z"
REVIEWED_AT = "2026-09-26T00:00:00Z"
EXPIRES_AT = "2026-09-27T00:00:00Z"


def _test_candidate() -> dict:
    key = "epk:v1:resolved:" + "a" * 64
    return {
        "schema_version": 1,
        "test_only": True,
        "subject": {
            "executor": "copilot",
            "model_id": "fixture-model",
            "role": "build",
            "report_role": "builder",
        },
        "profile_key": key,
        "cohort": {
            "benchmark_type": "issue-resolution",
            "deck_digest": "sha256:" + "b" * 64,
            "evaluator_revision": "sha256:" + "c" * 64,
            "deck_id": "fixture-deck",
            "loadout": "fixture-loadout-v1",
        },
        "coverage": {
            "state": "complete",
            "expected_encounters": ["encounter-a", "encounter-b"],
            "observed_encounters": ["encounter-a", "encounter-b"],
        },
        "source": {
            "producer": "test-fixture",
            "schema_version": 2,
            "revision": "fixture-revision",
            "report_digest": "sha256:" + "d" * 64,
            "artifact_digest": "sha256:" + "e" * 64,
        },
        "profile_observation": {
            "state": "complete",
            "resolved_key": key,
            "actual_key": "epk:v1:actual:" + "f" * 64,
            "requested": {"effort": {"state": "known", "value": "high"}},
            "resolved": {"loadout": {"state": "known", "value": "fixture-loadout-v1"}},
            "observed": {"loadout": {"state": "known", "value": "fixture-loadout-v1"}},
        },
        "measurement": {
            "verdict": "pass",
            "evaluated_at": "2026-09-25T20:00:00Z",
            "measured": {"clear_rate": 1.0},
            "unknown": {},
        },
        "mapping": {"version": "qualification-report-mapping/v1"},
    }


def _import_test_candidate(
    store: QualificationStore,
    *,
    expected_revision: int = 0,
    idempotency_key: str = "test-candidate",
) -> dict:
    result = store.import_test_candidate(
        _test_candidate(),
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
    )
    envelope = json.loads(
        (store.paths.candidates_root / f"{result['candidate_id']}.json").read_text(encoding="utf-8")
    )
    return envelope["payload"]


def _review(
    store: QualificationStore,
    candidate: dict,
    *,
    expected_revision: int,
    idempotency_key: str = "test-review",
    reviewed_at: str = REVIEWED_AT,
    expires_at: str = EXPIRES_AT,
    now: str = NOW,
) -> dict:
    return store.review_candidate(
        candidate["candidate_id"],
        verdict="approved",
        reviewer="fixture-reviewer",
        reviewer_authority="test-only",
        policy_revision="qualification-policy-v1",
        reviewed_at=reviewed_at,
        expires_at=expires_at,
        test_only=True,
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
        now=now,
    )


def _approved_binding(store: QualificationStore) -> dict:
    candidate = _import_test_candidate(store)
    _review(store, candidate, expected_revision=1)
    return candidate


def _touch(path: Path, *, mtime: str) -> None:
    """在 ``path`` 建立一個小檔案，並把 mtime 明確設成 ``mtime`` 這個時間戳。

    不靠寫入當下的真實時間——落盤內容與 atime 無關緊要，只有 mtime 是這裡的
    證據來源，測試藉 ``os.utime`` 直接指定，不依賴真實時鐘。
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")
    parsed = datetime.fromisoformat(mtime.replace("Z", "+00:00")).astimezone(timezone.utc)
    stamp = parsed.timestamp()
    os.utime(path, (stamp, stamp))


def _jobs_registry_path() -> Path:
    return config_paths.coordinator_root() / "jobs.json"


def _quota_admission_decisions_path() -> Path:
    return config_paths.quota_admission_decisions_root() / "decisions.jsonl"


# --- _clock_evidence_floor 本身的機制 -----------------------------------------


def test_clock_evidence_floor_is_none_when_no_source_exists() -> None:
    floor, available = lifecycle._clock_evidence_floor()
    assert floor is None
    assert available is False


def test_clock_evidence_floor_skips_non_regular_files_without_raising() -> None:
    # coordinator_root/jobs.json 本身是一個目錄而非檔案：stat 會成功，但
    # S_ISREG 判定會把它濾掉，視為「這個來源沒有證據」，不當掉、不誤用。
    _jobs_registry_path().mkdir(parents=True, exist_ok=True)
    floor, available = lifecycle._clock_evidence_floor()
    assert floor is None
    assert available is False


def test_clock_evidence_floor_takes_the_max_of_available_sources() -> None:
    _touch(_jobs_registry_path(), mtime="2026-09-27T00:00:05Z")
    _touch(_quota_admission_decisions_path(), mtime="2026-09-26T10:00:00Z")
    floor, available = lifecycle._clock_evidence_floor()
    assert available is True
    assert floor == datetime(2026, 9, 27, 0, 0, 5, tzinfo=timezone.utc)


# --- 整合進 qualification 查詢 -------------------------------------------------


def test_rollback_before_first_query_fails_closed_when_other_evidence_is_later(
    tmp_path: Path,
) -> None:
    """#1097 核心場景：真實時間已過 expires_at、回撥前無 qualification 查詢，
    但其他 Manager-only durable 來源（jobs.json）晚於 expires_at——查詢必須
    fail-closed，即使呼叫端傳入的（回撥後）now 本身還沒到 expires_at。
    """

    store = QualificationStore(root=tmp_path / "state")
    candidate = _approved_binding(store)

    # 回撥前，Manager 的 job registry 已經因為正常派工在真實過期之後又寫過一次。
    _touch(_jobs_registry_path(), mtime="2026-09-27T00:00:05Z")

    # 這是「第一次」qualification 查詢，且發生在時鐘回撥之後：now 落在
    # reviewed_at 與 expires_at 之間，單看 watermark 完全看不出真實時間已經
    # 越過 expires_at。
    rolled_back_now = "2026-09-26T12:00:00Z"

    status = store.qualification_status(
        "copilot", "fixture-model", candidate["profile_key"], "build",
        now=rolled_back_now, allow_test_receipts=True,
    )
    assert status["state"] == "expired"
    assert status["reason"] == "clock-evidence-expired"
    assert status["clock_evidence"] == "clock-evidence-checked"

    assert store.query_qualification(
        "copilot", "fixture-model", candidate["profile_key"], "build",
        now=rolled_back_now, allow_test_receipts=True,
    ) is None


def test_missing_clock_evidence_keeps_prior_behavior_with_unknown_reason(
    tmp_path: Path,
) -> None:
    """完全沒有可用的其他時間證據時，行為必須與 #1097 之前一致（仍是
    approved），只多帶一個可機讀的 ``clock-evidence-unavailable`` 診斷，
    明示這次判定沒有第二層佐證、風險未知。
    """

    store = QualificationStore(root=tmp_path / "state")
    candidate = _approved_binding(store)

    assert not _jobs_registry_path().exists()
    assert not _quota_admission_decisions_path().exists()

    active = store.query_qualification(
        "copilot", "fixture-model", candidate["profile_key"], "build",
        now="2026-09-26T12:00:00Z", allow_test_receipts=True,
    )
    assert active is not None
    assert active["state"] == "approved"
    assert active["clock_evidence"] == "clock-evidence-unavailable"


def test_unreadable_clock_evidence_is_treated_as_absent_not_a_crash(
    tmp_path: Path,
) -> None:
    """證據來源存在但不是一般檔案（模擬「讀不到／不可信」）：不得讓查詢當掉，
    也不得放寬既有 watermark 判定——等同完全沒有證據。
    """

    store = QualificationStore(root=tmp_path / "state")
    candidate = _approved_binding(store)

    # jobs.json 這個位置被別的東西占住（目錄而非檔案）：stat 成功但不是
    # regular file，_clock_evidence_floor 會跳過它，不會拋例外。
    _jobs_registry_path().mkdir(parents=True, exist_ok=True)

    active = store.query_qualification(
        "copilot", "fixture-model", candidate["profile_key"], "build",
        now="2026-09-26T12:00:00Z", allow_test_receipts=True,
    )
    assert active is not None
    assert active["state"] == "approved"
    assert active["clock_evidence"] == "clock-evidence-unavailable"


def test_evidence_earlier_than_expiry_does_not_affect_active_approval(
    tmp_path: Path,
) -> None:
    """有證據可讀，但證據本身也還沒到 expires_at：不該被錯誤地拉高判定，
    仍然正常 approved，只是 clock_evidence 標成已檢查過。
    """

    store = QualificationStore(root=tmp_path / "state")
    candidate = _approved_binding(store)
    _touch(_jobs_registry_path(), mtime="2026-09-26T18:00:00Z")

    active = store.query_qualification(
        "copilot", "fixture-model", candidate["profile_key"], "build",
        now="2026-09-26T20:00:00Z", allow_test_receipts=True,
    )
    assert active is not None
    assert active["state"] == "approved"
    assert active["clock_evidence"] == "clock-evidence-checked"


def test_forward_clock_expiry_is_unaffected_by_clock_evidence(tmp_path: Path) -> None:
    """時間正常前進（沒有回撥）時，即使其他證據也晚於 expires_at，過期判定
    仍走原本的 ``receipt-expired`` 原因，不會被誤標成「靠證據才判定」。
    """

    store = QualificationStore(root=tmp_path / "state")
    candidate = _approved_binding(store)
    _touch(_jobs_registry_path(), mtime="2026-09-27T00:00:05Z")

    status = store.qualification_status(
        "copilot", "fixture-model", candidate["profile_key"], "build",
        now="2026-09-27T00:00:10Z", allow_test_receipts=True,
    )
    assert status["state"] == "expired"
    assert status["reason"] == "receipt-expired"
    assert status["clock_evidence"] == "clock-evidence-checked"


def test_evidence_never_lowers_the_existing_watermark_regression_check(
    tmp_path: Path,
) -> None:
    """既有的 per-binding watermark 回退判定不能被證據放寬：一旦某次查詢已經
    記錄過較晚的 watermark，之後帶著更早 now 的查詢仍必須 fail-closed。
    """

    store = QualificationStore(root=tmp_path / "state")
    candidate = _approved_binding(store)

    first = store.query_qualification(
        "copilot", "fixture-model", candidate["profile_key"], "build",
        now="2026-09-26T23:00:00Z", allow_test_receipts=True,
    )
    assert first is not None

    rolled_back = store.query_qualification(
        "copilot", "fixture-model", candidate["profile_key"], "build",
        now="2026-09-26T12:00:00Z", allow_test_receipts=True,
    )
    assert rolled_back is None
    assert store.qualification_status(
        "copilot", "fixture-model", candidate["profile_key"], "build",
        now="2026-09-26T12:00:00Z", allow_test_receipts=True,
    )["reason"] == "clock-regression"
