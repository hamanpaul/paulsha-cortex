---
status: accepted
work_item: fix-superseded-terminal-replay
---

# Superseded terminal identity and replay protection

## Requirements

Authority：[#497](https://github.com/hamanpaul/paulsha-cortex/issues/497)，完整保留 [#832 更正 todo](https://github.com/hamanpaul/paulsha-cortex/blob/60a3ffa867377b0c86fa2f10e91fb9820d91938c/docs/superpowers/workstreams/fix-superseded-terminal-replay/todo.md)。accepted 僅是進件規格，Red 必須保留；人工 split 候選不等於 #833 已產生 child 的 runtime 證據。

1. **S01 Durable identity**：additive job/attempt superseded 與已完整 consumed 狀態可持久、可稽核、可 CAS/冪等更新；舊 row 缺欄位可讀，不新增 jobs.json 根欄位。
2. **S02 Atomic recovery**：驗證原 builder/reviewer 綁定後，recover-pre-candidate 的同一次 durable 更新標記舊 jobs、slice/gate→pending，真正清除 builder_job_id/reviewer_job_id/candidate；不能以 `update_slice(None)` 假裝已清除。
3. **S03 CAS and retry**：validation、state/history、supersession 一起成功或回復；stale recovery 不清除新 attempt。同一有身分 request 重送在 action gate 前可辨認已成功結果，不放寬其他 pending slice 的 allowed actions、不重追加 audit。
4. **S04 Replacement and consumption**：abandon、retry-build→repin、新 binding 的真實取代路徑持久標記舊 job；正常收割只在所需 proof 與交易完成後標 consumed，不能先標完再丟 completion evidence。
5. **S05 Early admission**：complete_tick 在解析 repo/branch HEAD/candidate/evidence path 前，skip superseded、已完整 consumed、或所屬 slice 存在但已非 current builder/reviewer 的 terminal job。真正 missing slice 保持 fail-closed，明確 workflow-lane unbound 例外保留。
6. **S06 No stale side effect**：skip 不寫 evidence/handoff、不查 branch HEAD、不改 slice/action/history；舊 job/evidence 仍可讀。Current attempt 的正常 completion/review 和真正缺 proof 診斷不倒退，所有已列管 terminal 入口有 executable invariant regression。
7. **S07 Reachable recovery fixture**：failed terminal builder、needs_human、candidate=None、明確 PSC_REPO_ROOT、合法 manifest；先證明 recover 在 allowed actions，再驗成功、bindings 真清除、舊 job superseded。合法 SHA dirty slice 不可拿來假造可 recover fixture。
8. **S08 Completion and dispatch**：recover 後 complete_tick 不回舊 job 為 completed/errors、不寫舊 candidate evidence、不 quarantine，slice 保持 pending/history 不增；獨立完整 tick fixture 在其他 gates 合法時恰好派一個新 builder。Complete-only 不承諾派工。
9. **S09 Manifest and restart**：至少測 needs_human＋空 verification_evidence_path＋verification-runner-error 的 manifest 回 None 路徑，另測 manifest missing/corrupt/已被新 job 取代；fresh JobRegistry/restart 仍 skip，不依賴單槽 manifest 保證已消費。
10. **S10 Multi-attempt**：同 slice 多 terminal、manifest 指向最新 job，只允許 current 且未完成者終局化。合法 candidate 的 dirty slice 走 retry-build→repin，涵蓋 builder/reviewer 取代，不把所有 unbound job 當合法或全丟棄。
11. **S11 Completed stability**：completed/passed slice 重啟後連續 10 ticks，CompletionRecord 與引用 evidence bytes 不變、不倒退 needs_human、不鎖下游；非 dirty recheck 的 superseded needs_human 靜止。合法 dirty recheck append 的 #496 殘餘不算本票缺陷。
12. **S12 Failure matrix**：focused tests 覆蓋 stale CAS、原子寫入故障、重送、新舊 row、missing slice fail-closed、current attempt completion、歷史可讀。不可放寬 immutable writer 或靠未設定 repo root 讓修前提前 error 假綠。
13. **S13 Delivery**：Cortex RED/GREEN、全套/CI/policy、獨立 review、exact-head merge、loaded runtime restart/proof 穩定分帳。同步 fragment 與 CHANGELOG；未過必要依賴與 parent 全部 AC，不關 #497。

## Evidence

基底 `79ba644780bf1c697c722ac24a297e7d02416100`，相關 code/tests 與 `60a3ffa8` 相同。

- [manifest helper](https://github.com/hamanpaul/paulsha-cortex/blob/79ba644780bf1c697c722ac24a297e7d02416100/paulsha_cortex/coordinator/manager.py#L164-L215) 保留 job_id；不是 recover 一律清掉 manifest。 [terminal enumeration](https://github.com/hamanpaul/paulsha-cortex/blob/79ba644780bf1c697c722ac24a297e7d02416100/paulsha_cortex/coordinator/manager.py#L2131-L2170) 先依 manifest，再找現行綁定。
- [update_slice](https://github.com/hamanpaul/paulsha-cortex/blob/79ba644780bf1c697c722ac24a297e7d02416100/paulsha_cortex/coordinator/registry.py#L1561-L1586) 的 None 為未提供；[recover call](https://github.com/hamanpaul/paulsha-cortex/blob/79ba644780bf1c697c722ac24a297e7d02416100/paulsha_cortex/coordinator/manager.py#L1738-L1828) 因此未真正清 binding。
- [missing-slice-proof 分支](https://github.com/hamanpaul/paulsha-cortex/blob/79ba644780bf1c697c722ac24a297e7d02416100/paulsha_cortex/coordinator/manager.py#L2263-L2283) 需與 superseded unbound 分開；不能借題移除真正失聯 job 的診斷。
- [既有 reclaim/manifest 測試](https://github.com/hamanpaul/paulsha-cortex/blob/79ba644780bf1c697c722ac24a297e7d02416100/tests/test_pre_candidate_recovery.py#L70-L235) 尚未驗 bindings 清空＋restart/complete/full tick 全鏈。

## Non-goals

不重做 #383 fanout、#496 dirty no-op、#501 contract/evidence hash 分離或 bucket index、#821 persist、#547 target selection。不可變 evidence 位址保持現行契約；原 issue「合法多次 observation 才需 attempt 位址」不構成本票授權普遍改址。本票消除不合法 replay，不新增自動 recovery/retry，不刪 job/證據，不宣稱跨 process writer lock 已由本票解決。
