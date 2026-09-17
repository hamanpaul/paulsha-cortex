---
status: accepted
work_item: reviewer-authority-hashes-echo
---

# Reviewer authority_hashes 回聲不得整份丟棄通過的 review

## Boundary

- Issue：`hamanpaul/paulsha-cortex#922`。
- 範圍：`coordinator/manager.py` 的 review terminal 採信（`terminalize_workflow_job` review 分支，
  `required` key-set 與 `expected_authority_hashes` 比對）、review 卡 prompt 的 `terminal_schema`
  （`_workflow_job_prompt` review 分支）、以及 `resume-workflow-failed` 對 review terminal 例外的
  診斷內容。verify 分支、`foreign_review.verify_authority_in_input_snapshot`／sandbox materialize
  的逐檔 sha256 驗證、其他 required keys（`schema_version`／`kind`／`reason`／`findings`／`reports`）
  與 `status` 的 non-passing 攔截一律不動。
- 不放寬任何 authority 綁定：模型**有帶** `authority_hashes` 時仍精確比對，帶錯值仍 fail-closed；
  本票只處理「缺席」。不新增 retry 迴圈、不改 `retry-card` 語意、不動 durable schema。

## 現場證據

run `workflow-22b00a5937e88c0090ab`（#826）同一 candidate 派 4 次 claude/sonnet reviewer，3 次 envelope
缺 `authority_hashes`（其餘五鍵齊全、`findings: []`、verdict passed），每次都
`ValueError: workflow review terminal schema invalid` → run `needs_human: resume-workflow-failed`，
`evidence_refs` 空；operator 只能讀 log 後 `retry-card`。`_workflow_job_prompt` 已把期望值放進 `fixed`
要求逐字照抄（註解自述「實測 2/2 省略」），證明 prompt 端無法根治。該欄位是 tautology：
Manager 端 `expected_authority_hashes` 與 prompt 給模型的值同源（job `workflow_input_snapshot`）。

## Tasks

- [x] **T1 tests／RED**：新增 `tests/test_review_authority_hashes_echo_922.py`：以帶 `planning-authority`
      snapshot 的 review job，terminal envelope（a）缺 `authority_hashes`、其餘合法且 findings 空，
      現行必須 `ValueError("workflow review terminal schema invalid")`（RED 基線）；（b）帶正確值；
      （c）帶錯誤值；（d）帶部分鍵。修後：(a) 採信、verdict payload 的 `authority_hashes` 由 Manager 依
      job snapshot 補齊並標 `authority_hashes_source: "manager-snapshot"`；(b) 採信且 source 為
      `reviewer-echo`；(c)(d) 維持 fail-closed 且錯誤訊息點名不符的 path。
- [x] **T2 source／採信端**：`terminalize_workflow_job` review 分支把 `authority_hashes` 從 exact
      key-set 的必要鍵改為選填：缺席時以 `expected_authority_hashes` 補進 `verdict_payload`／evidence，
      有帶時沿既有精確比對；`_fold_agy_key_value_map` 路徑不變。不得影響 verify 分支。
- [x] **T3 source／prompt 端**：review 卡 `terminal_schema` 保留 `authority_hashes` 於 `fixed`（期望值
      仍提供），但從 `required` 移除並註明「缺席時由 Manager 以 snapshot 補齊；帶值必須逐字相符」；
      更新原「實測 2/2 省略」註解為本票結論。無 planning-authority 時 prompt 逐字不變。
- [x] **T4 source／診斷**：review terminal 採信失敗（任何 `ValueError`）落到 run 的
      `resume-workflow-failed`／`terminalize-workflow-job-failed` diagnostic 時，context 附
      `envelope_keys`（排序）、`findings_count`、`reason_head`（≤200 字）與 `job_log_path`；
      內容不可解析時附 `envelope_parse_error`。不改既有 reason 字串。
- [x] **T5 tests／回歸**：`tests/test_terminal_result_contract.py`、`tests/test_workflow_production_wiring.py`
      與其他 review terminal 相關測試維持綠燈；補 T4 的 diagnostic 斷言；non-passing `status`
      仍被攔截。
- [x] **T6 documentation**：`docs/unified-work-lifecycle.md` 的 review 採信段補「`authority_hashes`
      缺席由 Manager 補齊、帶值必須相符」一句；新增 `changelog.d/reviewer-authority-hashes-echo.md`
      並同步 `CHANGELOG.md [Unreleased]`。
