---
status: accepted
work_item: reviewer-authority-hashes-echo
---

# Tasks

- [x] T1 tests/RED：新增 `tests/test_review_authority_hashes_echo_922.py`，覆蓋 planning-authority
      snapshot 下 review terminal 的缺席／正確回聲／錯誤值／部分鍵四種 `authority_hashes`
      情境，並把缺席補齊與 source 標記寫成 RED 斷言。
- [x] T2 source／採信端：`terminalize_workflow_job` review 分支把 `authority_hashes` 從 exact
      key-set 的必要鍵改為選填；缺席時以 job snapshot 補進 verdict/evidence，有帶時維持精確比對。
- [x] T3 source／prompt 端：review 卡 `terminal_schema` 保留 `authority_hashes` 於 `fixed`，
      但從 `required` 移除並註明缺席補齊與逐字相符規則。
- [x] T4 source／診斷：review terminal 採信失敗的 `resume-workflow-failed`／
      `terminalize-workflow-job-failed` diagnostic 補 `envelope_keys`、`findings_count`、
      `reason_head`、`job_log_path`／`envelope_parse_error`。
- [x] T5 tests／回歸：既有 review terminal 相關測試維持綠燈，並補上 T4 diagnostic 斷言與
      non-passing `status` 攔截覆蓋。
- [x] T6 documentation：更新 `docs/unified-work-lifecycle.md`、`changelog.d/reviewer-authority-hashes-echo.md`
      與 `CHANGELOG.md` `[Unreleased]`。
