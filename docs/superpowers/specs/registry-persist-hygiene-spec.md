---
status: accepted
work_item: registry-persist-hygiene
---

# Registry persistence hygiene

## Requirements

Authority：[#821](https://github.com/hamanpaul/paulsha-cortex/issues/821)，完整保留 [#832 更正 todo](https://github.com/hamanpaul/paulsha-cortex/blob/60a3ffa867377b0c86fa2f10e91fb9820d91938c/docs/superpowers/workstreams/registry-persist-hygiene/todo.md)。#832 已將 issue 的錯誤 append fixture、mtime-only sweep、limit=1 與 rollback 範圍校正，本規格以該定案為準。

1. **H01 Byte digest no-op**：穩定 UTF-8 JSON bytes SHA-256 相同且 state file 仍存在時，不 writer/mkstemp/replace、不改 mtime/inode；檔案被刪仍重建。Digest 不壓掉真正新 timestamp/history。
2. **H02 Writer compatibility**：`_write_payload_atomically(payload)` 保留 dict 第一位置參數。若新增 optional serialized，既有 fault wrappers 同步傳送但保留 fault 條件與 rollback 斷言；v1 dict-only migration 不壞，不吞 TypeError。
3. **H03 Load and rollback digest**：_load 在 normalization 能 persist 前先採原磁碟 bytes digest；成功 durable 寫入才採新 digest。Rollback `_load` 回復舊 digest，不能讓失敗候選誤擋下一次合法寫入。
4. **H04 Bounded histories**：default=500，constructor/env 可覆寫、僅正整數。evidence_history/evaluation_history/actions 共用 helper；limit≥2 保留首筆＋最新 limit−1，limit=1 保留最新；每丟一項累計 row-level history_truncated，不重設既有計數。
5. **H05 Load and copy**：舊 row 缺 counter 可讀，超限在複本正規化以保留原 payload 的比較，首次載入落盤、二次不再改寫；nested counter copy 不共享 live row。非法 limit/counter 型別/負值明確拒絕；root schema 不變。
6. **H06 Rollback efficiency**：依 #832 accepted todo，直接 hardlink state inode 作 rollback，不能把 v1「先 copy tmp 再 link 命名」冒稱零複製。EPERM/EXDEV/其他不支援 hardlink OSError fallback 原 bytes+fsync；保留 replace 後 fsync 故障 rollback 語意。
7. **H07 Ownership-safe startup sweep**：僅 constructor、state 目錄單層、指定 tmp patterns、grace≥30 秒且**可證 inactive writer 或獨占權威**才清。mtime 不構成 ownership 證明；未知／活躍略過並診斷。不可遞迴／跟 symlink／刪目錄、state、v1 backup、operator backup、必要證據；reload/persist 不 sweep。
8. **H08 Persistence matrix**：連續三次 persist 及真實同狀態 update_status 不建 tmp、不改 mtime/inode；deleted state 重建；writer fault/rollback/normalization digest 正確。保留 #501 normalization 與 released claim-key load 回歸。
9. **H09 Real append fixture**：limit=5，12 次 record_action(evidence_refs) 使 actions/evidence_history=5、首筆/第12筆保留、dropped=7；evaluation_refs 同驗。涵蓋 limit=1/2、超限 load、二次 no-op、counter 延續、copy 隔離，不用 update_slice 假裝 append。
10. **H10 Filesystem negatives**：hardlink 成功、EPERM/EXDEV fallback、replace 前後 fault；sweep 保留活躍/未知/新檔/symlink/subdir/人工備份，只清已證失活且逾 grace 匹配檔。缺目錄正常建置；讀 state 無權仍維持原錯誤，不因 sweep 容錯偽裝載入成功。
11. **H11 Delivery and retention**：Cortex focused RED/GREEN、fault/rollback、全套/CI/policy、review/merge、loaded runtime 寫入次數分帳；fragment/CHANGELOG 同步。輪替不等於完整 archive；current/completion 必要 evidence 仍可追、不可清。需要全歷史的部署在長期 archive 完成前，由已授權主流程保留原 registry 備份才啟用截斷。

## Evidence

基底 `79ba644780bf1c697c722ac24a297e7d02416100`，相關 code/tests 與 `60a3ffa8` 相同。

- [registry load](https://github.com/hamanpaul/paulsha-cortex/blob/79ba644780bf1c697c722ac24a297e7d02416100/paulsha_cortex/coordinator/registry.py#L353-L484)、[writer/persist](https://github.com/hamanpaul/paulsha-cortex/blob/79ba644780bf1c697c722ac24a297e7d02416100/paulsha_cortex/coordinator/registry.py#L550-L641)：每次 persist 呼叫 writer；rollback 目前複製 bytes。
- [history loader/copy](https://github.com/hamanpaul/paulsha-cortex/blob/79ba644780bf1c697c722ac24a297e7d02416100/paulsha_cortex/coordinator/registry.py#L944-L987)、[record_action](https://github.com/hamanpaul/paulsha-cortex/blob/79ba644780bf1c697c722ac24a297e7d02416100/paulsha_cortex/coordinator/registry.py#L1591-L1676)：append 的真實入口與 row copy seam。
- [normalization regression](https://github.com/hamanpaul/paulsha-cortex/blob/79ba644780bf1c697c722ac24a297e7d02416100/tests/test_coordinator_registry_headless.py#L375)、[publication fault wrapper](https://github.com/hamanpaul/paulsha-cortex/blob/79ba644780bf1c697c722ac24a297e7d02416100/tests/test_planning_publication_transaction_536.py#L551-L572) 與 `tests/test_workflow_production_wiring.py:5773` 的 wrapper 必須保留原驗收語意。

## Non-goals

不改 #496 dirty recheck 決策、#497 supersession、#501 bucket index authority、tick clock、_reload_if_changed 判定、跨 process lock/#818、字串 schema migration、instance decommission、per-slice 分檔/壓縮。只加 row 欄位、不 bump schema、不新增 jobs.json 根鍵。長期全歷史 archive/retention 未由本票提供，保留 #829 gap；本次進件不刪 registry 或任何現場 tmp。
