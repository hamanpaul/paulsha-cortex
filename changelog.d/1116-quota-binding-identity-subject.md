# 1116-quota-binding-identity-subject

- **`#1116`：quota binding 新增 executor＋model_id 穩定 identity subject**——同一個
  builder 身分（例如 codex／gpt-6-luna）在不同卡片依 launch contract／
  requirements 解析出不同的 execution profile resolved key 時，`quota-pools.json`
  過去只能以 `subject.kind == "profile"`／`"group"` 逐一列舉 resolved key，卡片
  deck 一改 binding 就無聲失效（shadow 下變成 `unmanaged`）。`quota_observation.
  parse_binding()` 新增第三種 `subject.kind == "identity"`（`{"kind": "identity",
  "executor": ..., "model_id": ...}`），直接綁定操作者本來就知道、且跨卡片不變
  的 executor＋model_id；未知的第三種 kind 仍一律拒絕（沿用 #836 既有 parser
  的加法、嚴格驗證模式）。
- **`#1116` 優先序**：`quota_admission.pools_for_profile()`／`assess_candidate_quota()`
  比對優先序為 resolved profile key 精確綁定（`profile`／`group`）＞ executor＋
  model_id 穩定 identity 綁定；命中精確綁定時只採用精確綁定的 pool/window 集合，
  不與 identity 綁定的結果合併，同一候選同時命中兩者不會被重複計算成兩份 pool
  需求。既有以 resolved key 綁定的設定完全相容，不帶 `executor`／`model_id`
  參數的既有呼叫端行為逐字不變。`quota_shadow.record_terminal_usage()` 同步
  支援 identity binding，否則 admission 用 identity binding 判定可行、job 終局
  時卻扣不到消耗。`quota_collectors`／`quota_sources` 的 collector_targets 精確
  比對刻意不新增 identity 路徑（見兩處新增的文件字串：collector_targets 的
  `profile_key` 是設定載入當下已比對過一次的手動填寫值，不是要替動態候選
  即時找 binding；identity binding 仍可與既有 binding 並存於同一份設定檔）。
- **`#1116` 可觀測性**：`CandidateAssessment.binding_kind`／`AdmissionDecision.
  selected_binding_kind`（`"exact"`／`"identity"`／`"none"`，比照 #840 選填欄位
  加法模式，缺席視為 unknown，不需要 schema bump）記錄候選『目前是靠哪一種
  binding 涵蓋』；`decision_projection` 的 `classification.binding` 投影成
  `bound-exact`／`bound-identity`／`binding-missing`／`unknown`，讓 `cortex work
  show`／`inspect status` 能區分『完全沒有任何 binding』與『有 binding 但餘量
  unknown』。新增唯讀 `cortex quota bindings --report --config <path> [--store
  <path>] [--json]`（`quota_admission.unbound_profile_keys_report()`）：彙總
  decision store 裡曾經派工用到、但用**目前**設定重算仍然沒有任何 binding 涵蓋
  的 resolved profile key，只讀 store／設定檔，不寫任何狀態。
- **`#1116` 測試**：新增 `tests/test_quota_binding_identity_1116.py`（27
  項），涵蓋 identity subject 解析／拒絕未知 kind、單一 identity binding 涵蓋
  同 identity 兩個不同 resolved key、精確綁定優先且不重複計算、
  `assess_candidate_quota`／decision receipt／projection 的 binding_kind
  三態、`unbound_profile_keys_report` 重算不信任舊快照、collector_targets 與
  identity binding 並存的相容性、`record_terminal_usage` 的 identity 比對，以及
  `cortex quota bindings --report` CLI 端到端。RED（暫時還原 production 檔至
  `git show HEAD:<path>`）→ GREEN 全數確認。
