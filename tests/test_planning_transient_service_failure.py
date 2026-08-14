"""planner launcher 暫時性服務失敗的分類與診斷（2026-08-14 實測）。

現場：run `workflow-88d089d71416a754dda8`（work_id `fix-instance-config-isolation`）
的 define 失敗於：

```
primary-integration-malformed: ValueError: planning launcher returned no JSON object
classification: content
```

追根究柢是 agy 服務暫時回 `Error: Eligibility check failed: UNAVAILABLE (code 503)`
——**印錯誤文字但 exit 0**。launcher 信任 exit 0 去 parse stdout、找不到 JSON，而：

1. 錯誤文字不進 reason 也不進 evidence（temp_dir 一併丟棄），operator 只看到
   「no JSON object」六個字；
2. 失敗被預設分類 `content` → `recover-planning` 被 #393 fail-closed 禁止 →
   一個十分鐘後自癒的 503（同一指令重跑即成功）成為永久死路，唯一出口 abandon。

這是 transient-誤判-死路模式的第五次命中（#500、#507 的 content 誤分類同族）。

本檔釘住兩個修正：
- `_extract_json` 的 no-JSON 失敗必須帶 stdout 截斷片段（503 當場可見）；
- `_is_planning_transient_service_failure` 把服務層暫時性樣態改判 `environment`
  （與 #416 的殘留例外同路，recover-planning 因此可用），且判準刻意窄——
  模型「內容不從」（回散文、schema 不合）維持 `content`。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from paulsha_cortex.coordinator import manager
from paulsha_cortex.coordinator.planning_runtime import _extract_json


# ---------------------------------------------------------------------------
# _extract_json：no-JSON 失敗必須帶輸出片段
# ---------------------------------------------------------------------------


def test_no_json_error_carries_stdout_snippet(tmp_path: Path) -> None:
    stdout = "Error: Eligibility check failed: UNAVAILABLE (code 503): The service is currently unavailable."
    with pytest.raises(ValueError) as excinfo:
        _extract_json(stdout, tmp_path / "absent.json")
    message = str(excinfo.value)
    assert "returned no JSON object" in message
    assert "UNAVAILABLE (code 503)" in message, "錯誤文字必須進 reason，不得只留六個字"


def test_no_json_error_snippet_is_truncated(tmp_path: Path) -> None:
    stdout = "x" * 500
    with pytest.raises(ValueError) as excinfo:
        _extract_json(stdout, tmp_path / "absent.json")
    # reason 上游會再截 160；這裡確保片段本身有界，不把整包輸出塞進例外。
    assert len(str(excinfo.value)) < 260


def test_no_json_error_with_empty_output_says_so(tmp_path: Path) -> None:
    with pytest.raises(ValueError) as excinfo:
        _extract_json("", tmp_path / "absent.json")
    assert "<empty output>" in str(excinfo.value)


def test_valid_json_still_extracts(tmp_path: Path) -> None:
    assert _extract_json('{"questions": []}', tmp_path / "absent.json") == {"questions": []}


# ---------------------------------------------------------------------------
# 分類判準：服務層暫時性 → environment；內容不從 → 維持 content
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reason",
    [
        # 實際現場（agy 503，經 _extract_json 片段與 planning.py 前綴包裝後的樣貌）
        "primary-integration-malformed: ValueError: planning launcher returned no "
        "JSON object: Error: Eligibility check failed: UNAVAILABLE (code 503): The service is",
        "question-pack-malformed: ValueError: planning launcher returned no JSON "
        "object: 429 Too Many Requests",
        "secondary-output-malformed: ValueError: planning launcher returned no JSON "
        "object: upstream connect error: connection timed out",
        "primary-integration-malformed: TimeoutExpired: command timed out after 300s",
        "secondary-output-malformed: ValueError: planning launcher returned no JSON "
        "object: model is overloaded, please retry",
    ],
)
def test_transient_service_failures_are_recognized(reason: str) -> None:
    assert manager._is_planning_transient_service_failure(reason) is True


@pytest.mark.parametrize(
    "reason",
    [
        None,
        "brainstorm-not-ready",
        # 模型內容不從：回散文不回 JSON——這是 content，不是服務錯誤。
        "primary-integration-malformed: ValueError: planning launcher result is not "
        "JSON: 我認為這個問題應該從三個面向來分析",
        # 標題/schema 類拒收維持 content。
        "primary-artifact-write-rejected: ValueError: required section missing: Requirements",
        "question-pack-malformed: KeyError: 'questions'",
    ],
)
def test_content_failures_stay_content(reason: str | None) -> None:
    assert manager._is_planning_transient_service_failure(reason) is False


def test_residue_and_transient_predicates_are_disjoint_paths() -> None:
    """兩個例外判準各自命中各自的情境，互不誤觸。"""

    residue = (
        "primary-artifact-write-rejected: ValueError: planning artifact lacks "
        "current planning authority: docs/superpowers/specs/x.md"
    )
    transient = (
        "primary-integration-malformed: ValueError: planning launcher returned no "
        "JSON object: UNAVAILABLE (code 503)"
    )
    assert manager._is_planning_authority_residue_failure(residue) is True
    assert manager._is_planning_transient_service_failure(residue) is False
    assert manager._is_planning_authority_residue_failure(transient) is False
    assert manager._is_planning_transient_service_failure(transient) is True
