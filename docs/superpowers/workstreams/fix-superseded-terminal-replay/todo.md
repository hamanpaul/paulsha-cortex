---
status: accepted
work_item: fix-superseded-terminal-replay
domain_breadth: 1
state_consistency: 2
invariant_count: 13
artifact_classes:
  - source
  - tests
  - documentation
---

# Superseded terminal job 的持久身分與防重播

## Boundary

- Issue：`hamanpaul/paulsha-cortex#497`；正式 work_id 保持不變。
- 本票修 completion 側 attempt 身分／supersession／consume 判定，讓已取代的
  terminal job 不再推導 candidate 或寫 missing-slice-proof，保護目前與已完成 slice。
- #383 的 `_manifest_still_blocks_fanout`／`dispatch_gate_scan` 與 manifest
  superseded 註記已存在，不重做 fanout 語意；manifest 單槽 job_id 不是完整冪等權威。
- 不放寬 evidence writer 不可變性／quarantine、不刪 job 或舊 evidence、不實作
  #496 dirty recheck 去重／#821 persistence、不新增自動 retry 或自動 recovery。
- #501 的 contract/evidence hash 分離已存在；本票維持正確 current evidence hash，
  不以修改 contract hash 或重寫 evidence 位址掩蓋 attempt 混淆。

Spec/design：`docs/superpowers/specs/fix-superseded-terminal-replay-{spec,design}.md`。
保留 #832 全部 parent AC。完整 parent 即使依 #831 定案 stability=0 仍須真實 sizing；Red 不降分，report 的人工候選不是 #833 自動 planner evidence。

## Tasks

- [ ] **T01 source／S01**：以 additive registry job／attempt 欄位持久記錄 superseded／已消費狀態，
      提供 CAS／冪等更新。沿用可稽核的 superseded_at／superseded_by／superseded_reason
      或等價具體結構，缺新欄位的舊 state 正常載入；不新增 jobs.json 根欄位。
- [ ] **T02 source／S02**：為 recover-pre-candidate 增加明確的 registry 原子動作：在驗證原 builder／
      reviewer 綁定仍相同後，同一次 durable 更新將舊 job 標 superseded、slice 轉
      pending、gate_state 轉 pending，並清掉 builder_job_id／reviewer_job_id／candidate。
      不再依賴 `update_slice(...=None)`：現行該 API 把 None 當未提供，不會清欄位。
- [ ] **T03 source／S03**：原子動作內的 validation／state／history 與 supersession 一起成功或回復；
      fault injection／CAS conflict 不得留下「slice pending 但仍綁舊 job」或「新
      attempt 綁定被舊 recover 清掉」的半套 durable 狀態。重複同一 recovery request
      不重複標記或寫 action；在 action gate 前處理有身分可驗的既有成功結果，
      不藉此放寬其他 pending slice 的 allowed actions。
- [ ] **T04 source／S04**：核對 abandon／retry-build→repin／新 attempt 綁定的取代路徑：當前綁定真的
      被取代時持久標記舊 job；正常 terminal 被成功收割後保存 consumed 身分，
      不依賴單一 manifest job_id 推測跨重啟已消費。新增標記不可讓尚未完整持久化的
      completion proof 被略過，需與相應交易邊界一致。
- [ ] **T05 source／S05**：`complete_tick` 對 terminal job 在解析 repo root／branch HEAD／candidate／
      evidence path 之前先 skip：已標 superseded／已完整消費者，或其 slice 存在
      但當前 builder/reviewer 綁定已非該 job。不能只檢查 `_slice_for_job` 回 None
      就掉進 missing-slice-proof；真正 slice 不存在的 job 保持現行 fail-closed 行為。
- [ ] **T06 source／S06**：skip 舊 job 不寫 evidence、不改 handoff、不解析 branch HEAD、不追加 slice
      action/history；舊 job／舊 evidence 仍能讀取。保留真正當前 attempt 正常
      completion/review 與缺 proof 診斷，並以 source invariant 測試涵蓋所有 terminal 入口。
- [ ] **T07 tests／S07**：recovery fixture 沿
      `tests/test_pre_candidate_recovery.py::test_recover_pre_candidate_supersedes_stale_handoff_manifest`：
      明設 `PSC_REPO_ROOT`、failed terminal builder、needs_human slice 且
      `candidate=None`，寫合法舊 manifest；先斷言 allowed actions 包含 recover，
      再執行 action 並斷言成功、bindings/candidate 真為 None、舊 job 已 superseded。
      不得使用帶合法 candidate SHA 的 dirty slice 呼叫 recover，那會被 action gate 擋下。
- [ ] **T08 tests／S08**：recovery 後執行 `complete_tick`，completed／errors 都不含舊 job，沒有對舊
      candidate 寫 evidence、沒有 quarantine，slice 維持 pending，history 不增。
      另在完整 `run_tick`／fanout fixture 中驗其他 gates 均合法時恰好派一個新 builder；
      `complete_tick` 本身不承諾派工。設定 repo root 避免測試提前 error 卻假綠。
- [ ] **T09 tests／S09**：manifest 回 None 的變體必測：gate_status=needs_human、
      verification_evidence_path=None、gate_reason=verification-runner-error；
      修前會重跑舊 job，修後仍跳過。再以 fresh JobRegistry 模擬 daemon restart，
      驗證 supersession／consumed 與 skip 持續有效。
- [ ] **T10 tests／S10**：同一 slice 的多個 terminal jobs（例如 -6／-8／-9，manifest 指向 -9）只讓
      真正目前綁定且尚未合法完成者終局化；unbound 舊 job 不推導 candidate。
      帶合法 SHA 的 dirty slice 走 `retry-build`→repin 的合法路徑，覆蓋舊 builder
      與 reviewer 被取代的情境；不可偷換成不可達的 recover fixture。
- [ ] **T11 tests／S11**：completed/passed slice 在 restart 後連續10個 ticks，completion record 與
      被引用 evidence bytes 不變，不倒退 needs_human、不反鎖下游 deps；needs_human
      但不符合 dirty recheck 的 superseded 情境保持靜止，驗的是舊 replay 來源，
      不將 #496 尚未實作的合法 dirty recheck append 當本票責任。
- [ ] **T12 tests／S12**：新增 `tests/test_superseded_terminal_replay_497.py` 或等價 focused 檔；涵蓋
      stale CAS、原子寫入故障、重複 recovery request、新舊 row 相容、真正 missing slice
      fail-closed、current attempt 正常完成與歷史可讀。不可放寬 immutable writer。
- [ ] **T13 documentation／S13**：補本正式 work_id changelog fragment 與 `CHANGELOG.md [Unreleased]`，透過
      Cortex 記錄 RED／GREEN、必要完整 gates、獨立 review、merge、runtime restart
      與 completion proof 穩定性的證據。accepted todo 不等於修正／測試／部署完成。

- [ ] **T14 documentation／CLI**：更新 recovery/runbook 對 current/superseded/consumed、合法 pre-candidate request replay 及缺 proof 的說明；保留 slice/work action 邊界。從 checkout 外實跑候選 `python3 -m paulsha_cortex.cli work start --help`、`python3 -m paulsha_cortex.cli run work --help`，另以隔離 request fixture 驗欄位/拒絕，不呼叫 live manager。
- [ ] **T15 tests／parent accounting**：純 completeness/contract/sizing 與完整 parent S01–S13 mapping 守門；Red 先受治理分解，每個 child 個別 sizing，不偽造 planner lineage。Parent closure 必須補齊所有 child、full pytest/CI/PR-context policy、review、exact-head merge 與 loaded runtime restart 證據；source 成功不替代 runtime。

## Evidence

- 舊紀錄中的 recover 後 pending 無法存活、unbound -8 汙染 bound -9 證據位址、
  manager 重啟後 completed slice 被回寫，是此票的回歸來源，不是此次 live 結果。
- `verification.write_verification_evidence` 發現內容衝突時 quarantine 並 fail-closed
  是必要保護；本票消除不合法第二次寫入，不將既有證據改成可覆寫。
- sentinel 收割／targeted complete 是觀測面後續工作，不在本票擴張操作權限。
