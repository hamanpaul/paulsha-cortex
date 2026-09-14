---
status: accepted
work_item: recovery-registry-receipt
issue: 862
---

# Recovery registry disposition 與 atomic receipt 規格

## Requirements

本文件屬於唯一 child owner [#862](https://github.com/hamanpaul/paulsha-cortex/issues/862)，`work_id: recovery-registry-receipt`，父項為 [#497](https://github.com/hamanpaul/paulsha-cortex/issues/497)。Root 已接受內容作 repository planning intake；不是 #833 自動分解結果或產品實作授權。母契約為 [spec](fix-superseded-terminal-replay-spec.md)／[design](fix-superseded-terminal-replay-design.md)。2026-09-08 root 已裁決原 OQ01/OQ02 並確認八件 fresh review PASS／0 未處置 BLOCKER/MAJOR，status 為 accepted。內容接受不等於 dispatch-ready：規劃 PR 尚未合併發布、source mapping/binding 尚未完成，T10 證據仍缺；產品須另經正式 Cortex gates。

Production scope **只有 `paulsha_cortex/coordinator/registry.py`**。domain_breadth=0，但 state_consistency=2：跨 job、slice、receipt、history 的 CAS、ABA 世代及單檔 durable transaction，不能視為純 schema 而降為 state=0/1。

下列 I01–I09 是九組驗收不變量；新增顯式 checkpoint 有真實 scope，故由八組增為九組，不刪原正負案例壓分。

1. **I01 Version 與 legacy truth**：既有缺少 A additive 欄位的 row 仍可辨識為 legacy/unversioned；不可當作 revision=0、current、superseded 或 consumed。A 的 read/reload 不得因缺新欄位而 backfill、persist 或改寫舊 bytes。未知 version／malformed 新欄位 fail-closed，不當成欄位不存在。root 已選最小相容範圍：現有 schema-v1 migration、#501 load-time repair 的政策與 fixtures 保持，不宣稱所有歷史格式讀取零寫入，全面 loader 改造不在本新增欄位承諾。
2. **I02 Immutable intent**：同一 registry 的 recovery/checkpoint 全部 receipt 共用不可重用的 `request_id`；digest 綁定各自 versioned payload。Recovery 綁 exact target/binding、actor/requested_by 與非空 required_steps；checkpoint 綁完整 observed legacy snapshot/fingerprint、job refs、provenance（D7）。相同種類、ID、digest 才是重送；同 ID 改種類/內容/target/要求均 conflict，包括 checkpoint ID 被拿來 recover。新 ID 即新 intent，不能由 actor/slice/action hash 推斷重送。
3. **I03 Exact CAS 與 ABA revision**：新建、已採 A schema 的 slice 有單調 `binding_revision`；recovery 必須明給 exact revision 與完整 binding snapshot。A 重驗目前 slice 與舊 builder/reviewer job refs，stale 或缺 pins 不改任何業務欄位。A→B→A 的相同 tuple 不恢復舊 revision；成功 repin 即使 tuple 相同也建立新世代。舊 row 只有 I09 的明示 checkpoint 可建立第一個 revision；read/prepare/commit/舊 mutator 不自動升級，舊 mutator 相容但維持 unversioned、不獲歷史或未來 ABA 保證，直到顯式 checkpoint。
4. **I04 Prepared 與 complete 分帳**：recovery receipt 可 absent→prepared→complete；prepared 只持久記錄 intent 與 expected snapshot，不能清 binding、設 pending、標 superseded/consumed 或增加成功 action。complete receipt 才能作為該 registry 操作成功的冪等答案；相同 complete 重讀不重新寫檔、不改時間、不重增 revision/history。prepared 不能因 restart、timeout 或重送自動升 complete。Checkpoint 使用獨立 context/receipt，由單次 transaction absent→complete；這只證明建立 checkpoint，絕不等於 recovery complete 或 consumed。
5. **I05 單 snapshot 原子 commit**：專用 recovery commit 在完成所有 revalidation 後，一次 durable 更新舊 job 的 supersession、slice/gate→pending、builder/reviewer/candidate→明確 null、新 revision、一次 action/history 與 complete receipt。validation/CAS/persist failure 回復此呼叫之前的 memory/durable snapshot；prepared 若先前已成功持久化則仍是 prepared。不能留下 pending 配舊 binding 或清掉新 attempt 的半套狀態，不改 `update_slice(None)` 的 no-op 相容語意。
6. **I06 Disposition truth**：job 的 supersession 與 consumption 分開儲存，包含 job/attempt、原因、actor/time、可追索的取代 identity 或 request receipt。exit=0、prepared 或 recovery complete 均不推定 consumed。consumption 需 B 提供已驗證的 required proof closure，A 只驗結構、exact job/binding 與一次持久更新；不能替 B 檢查 evidence 檔、執行 completion 或證明資源清理成功。既存 consumption 不因後續合法 supersession 被抹除。
7. **I07 Immutable history 與 restart**：receipt/disposition 的 nested values 在 get/list/return copy 中不可回寫內部狀態。舊 job、actions、evidence refs/hash 與歷史 receipt 保留；新操作只追加該操作必要的紀錄，不改既存 evidence bytes/address。fresh registry 能還原 receipt phase、revision、supersession/consumption 並拒絕舊 CAS；舊成功 receipt 可回歷史結果，但不得將 slice 改回該歷史結果。
8. **I08 Failure 與範圍守門**：version/pins/digest/target/ABA/phase/寫入故障皆有 deterministic 負例；atomic rename 前後、fsync/rollback、重啟與 duplicate request 的 durable/memory 結果一致。A 不新增 root state 欄位、不新增跨 process writer authority；若發現需改第二 production module、移除既有 migration 政策或接受不可信 legacy checkpoint，停止並交 root 重規劃，不能藉測試 stub 宣稱 S03 已端到端完成。
9. **I09 Explicit legacy checkpoint**：registry-only 原語接收 caller 固定的完整 observed legacy row/binding snapshot、canonical fingerprint、相關 job exact refs、獨立 versioned request identity/provenance。先查全 registry 同 ID receipt，再核對仍為 legacy、完整 snapshot/fingerprint/refs 不漂移；單一 durable transaction 新增 binding_version、revision=1 與不可變 checkpoint receipt，不清綁、不變更舊業務欄位/history、不標 consumed/superseded、不造過去 revision。stale、缺 snapshot、未知 version、ID 衝突、寫入失敗拒絕/rollback；成功後 exact replay（含重啟後）回原 receipt，不重 checkpoint、不重寫新世代。這是本次明示觀測的新起點，不是 legacy 歷史 ABA 的追證；C/B 必須重驗真 owner/proof 並透過明示 action 調用，A 不認證 actor 字串、不自動操作 live legacy。

## Parent Acceptance Mapping

| 母 AC | A 可交付的部分 | 必須留給後續 owner 的部分 |
|---|---|---|
| S01 | I01/I02/I06/I07/I09 durable disposition、receipt、顯式 checkpoint 與相容資料契約 | producer 正式給出 intent／proof；不把 checkpoint 當歷史身分證明 |
| S02 | I03/I05 registry CAS、實際清綁與原子 transition | B/C 合法 recovery gate、資源處理與呼叫 |
| S03 | I02–I05/I08/I09 跨種類 request ID、lookup、replay、CAS、rollback 原語 | B/C 在 action gate 前辨識 receipt、owner-bound checkpoint action；D2 傳原始 context，不以同 ID 冒充新版 recovery |
| S04 | I03/I06 replacement/consumption 儲存原語與 revision 更新 | B/C 真 abandon/retry/repin/binding 與 proof 完整接線 |
| S05–S06 | 提供可靠 disposition/current-binding 資料 | B early completion admission、零外部副作用及 missing-proof 例外 |
| S07–S08 | 只提供 registry-level fixture，不冒充合法 action fixture | B/C/D 真 allowed-action、complete_tick/full-tick 一次派工 |
| S09–S10 | I03/I07 fresh registry、multi-attempt/ABA fixture | B manifest 變體、current terminal 選擇、builder/reviewer 取代全鏈 |
| S11 | I07 既有歷史不重寫的局部保障 | B 重啟後十 tick、completion/evidence/downstream 穩定 |
| S12 | I01–I09 registry 範圍完整 failure matrix | B/C/D 公開入口與真 missing-proof/current completion 回歸 |
| S13 | A 自身的 RED/GREEN、review、CI、merge、installed 證據分帳 | 所有必要 child 與 parent live AC；不得只因 A 完成關 #497 |

本 child 的 OpenSpec lifecycle 由 `recovery-registry-receipt` 唯一 owner/source authority 持有，目標為 `openspec/changes/recovery-registry-receipt/{proposal.md,design.md,specs/**,tasks.md}`。正式 repository-intake planning 責任方須在產品 run start 前建立、審完、合併/發布並完成唯一 mapping/binding；own proposal/design self-contained 等價，tasks 的0/2/9、surfaces/rules 與 Todo 一致，不能期待產品 run 再 propose 補前置件。T10 只核對既存前置證據；candidate 對 frozen planning 只准既有 checkbox toggle，operator baseline immutable。只可封存自己的 change；產品 merge/installed/live、C/B/D 與母 closure 留 prose 分帳，不列 archive 前 checkbox、不 archive umbrella。本輪依 root 批准將八件內容改為 accepted，唯一 child owner 為 #862；尚未規劃 PR 合併發布、mapping/binding 或封存，T10 前置證據仍缺。現行 accepted sizing 為8 Red，#831 loaded 後的6 Yellow只作條件投影，不得派工。

## Non-goals

本輪僅更新原 spec/design/todo/report 加 own OpenSpec proposal/design/tasks 與 `specs/recovery-registry-receipt/spec.md` 共八檔的接受狀態與 #862 唯一 owner；status=accepted 只接受文件內容，不是產品實作、正式 intake 發布或 binding 授權。

不修改 manager、work_actions、control contract、CLI、daemon 或其 permission/action gates；不開 issue、不註冊 child、不改 umbrella 或 frozen 母輸入。不實作 filesystem reclaim、manifest/evidence writer、queue/done 對帳、模型/quota/retry、#383 fanout、#496 dedup、#501 evidence-address、#547 target selection、#818 跨 manager lock、#821 通用 persistence/retention。既有單 writer 範圍的 stale CAS 不是跨 process 安全證明。

## Closed Engineering Decisions

- **OQ01 closed by root，2026-09-08**：採 I09／D7 顯式 registry checkpoint，不新增權限或 public action 實作；C/B 後續擁有正式 owner-bound action、proof 與切換接線。Legacy 歷史 ABA 未知是明示殘餘，不假補 revision0/default 或回溯證明。舊 unpinned mutator 未被 A 接線修正，母 S03 仍需全部 children。
- **OQ02 closed by root，2026-09-08**：只保證 A additive 欄位不觸發 load-time 寫回，現有 v1/#501 migration 原樣保留。這個界線是明示相容裁決，不是把全面零寫入宣稱改成已證實。

兩項工程選擇已定案，沒有待 builder 自由決定的 legacy 升級權限；root 已接受 #862 文件內容，status=accepted。這不授與 actor/真人資格或產品 dispatch：planning PR 發布/source binding、T10 前置證據、真實 sizing/qualification 與產品驗收仍各自列帳。

## Evidence

來源基底 `b1b44bc476dc49481d8467c9595e7c4db3f1a87d`。目前 registry 無本草稿 receipt/revision API；本文件中的 API 名稱及 schema 是提案，不是存在宣稱。完整 source/test anchors、實跑 pure sizing 與未執行項目見 [review report](../../../reports/review/refine-recovery-registry-20260907.md)。
