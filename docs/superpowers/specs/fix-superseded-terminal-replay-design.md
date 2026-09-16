---
status: accepted
work_item: fix-superseded-terminal-replay
---

# Superseded terminal design

## Decisions

1. **Registry authority**：job/attempt 的 durable disposition 是 replay admission authority；manifest 只是既有投影/捷徑。Additive row 須含原 job、取代原因/actor/time、可驗 superseding identity 或 recovery request receipt；consumed 與 superseded 分開，不能因 exit=0 就推定 consumed。
2. **Current identity**：slice 存在但 builder/reviewer 綁定非該 job，屬非現行 attempt；row 缺新欄位時仍需核對 binding。不以 `_slice_for_job()==None` 同時代表「真正無 slice」和「舊 job」；workflow-lane explicit unbound 規則保持獨立。
3. **Atomic in one registry snapshot**：新增專用原子 recovery 操作，先 revalidate expected builder/reviewer/candidate/state 與 recovery identity，再 staged mutation、單次 persist；失敗恢復 memory/durable snapshot。清除使用此 explicit API，不改 update_slice 的既有 None semantics。
4. **Idempotent request boundary**：先識別相同已成功 receipt 再套 allowed-action gate，無合法 receipt 的 pending 不放行；CAS conflict 不能 last-writer-wins。這是既有 owner/單 writer 權威內的一致性，不宣稱 #818 多 manager 共享檔案問題已修。
5. **Producer integration**：核對並讓 slice/work 的 recover、abandon、retry-build→repin 及新 binding 更新消費同一 disposition 契約。Production 最小範圍為 registry、manager、work_actions；#547 的 target 選擇/雙入口廣泛整併仍原票負責。若 trace 證明需第四模組，先回 root 重評，不暗中擴張。
6. **Early completion guard**：terminal status 確定後，對已定位的 lane/current identity 做 admission，再碰 repo root、branch/candidate、evidence、handoff。被 skip 的 stale job 不執行那些副作用；malformed job/真正 missing proof 保持目前 fail-closed。
7. **Durable consumption**：只有 required evidence、slice/gate transition、completion/handoff 等該路徑必要 proof 已具足才可 consumed。跨檔 proof 寫入需可重入順序與 receipt/reconciliation；crash 後補缺失，不得用 consumed 先遮掉未完成 proof。不改 immutable writer/quarantine 規範。
8. **Reclaim is separate resource work**：filesystem reclaim 與 registry commit 不是同一原子寫入。沿既有 owner-aware reclaim，失敗保留具體 recoverable disposition，只有所有必要子步驟完成才回完整成功；不得因原子 row 更新就聲稱 workspace 也已處置。
9. **Tick ordering**：基底 `run_tick` 是 dispatch_gate_scan→fanout→complete（manager.py:2605–2655），非 completion→fanout。先獨立驗 recovery→complete 不污染，再以完整 run_tick 真實順序驗一次派工；不修改順序或 #383 gate 以滿足錯誤 fixture。
10. **Sizing**：domain_breadth=1（2–3 production 模組），state_consistency=2（job/slice/proof 跨持久物件 CAS、故障與 restart）。真實 fix-standard 分數依 pure helper；即使 #831 完整 stability=0，本 parent 仍 Red，不能整包派到 build。

## Verification

以 `tests/test_pre_candidate_recovery.py` 的 reachable candidate=None fixture 與真隔離 Git repo 作起點；新的 `tests/test_superseded_terminal_replay_497.py` 分別驗 builder、reviewer、workflow explicit-unbound、missing-slice、manifest missing/corrupt/指向新 job。Spy repo/candidate/evidence/handoff helpers 證實 stale skip 的零副作用，不只看 completed count。Fault injection 覆蓋 commit 前/後、proof 建立前/後、重複 request 與 late terminal。

## Decomposition and Compatibility

Report 列人工候選：registry disposition/CAS 原語、completion admission/consumption、recovery producer wiring/full-tick acceptance。每件都要有獨立 accepted authority、真實重新 sizing 與 parent S01–S13 coverage。這不是 #833 planner 產物；不註冊 child、不改 issue、不回填 depth/lineage。沒有全部必要 producer/consumer 接線與 parent end-to-end 驗證，不宣告 #497 完成。

舊 row 可讀但 unknown disposition 不自動升為 consumed；既有證據與 current verification hash 保留，新的 row validation／copy／persist 與 #821 future additive fields 相容。若合法跨 attempt evidence-address 需求另現，作為有界後續 gap，不用本票改址掩蓋 replay。
