---
status: accepted
work_item: recovery-registry-receipt
issue: 862
domain_breadth: 0
state_consistency: 2
invariant_count: 9
artifact_classes:
  - source
  - tests
  - documentation
---

# Recovery registry child A 工作計畫

## Boundary

唯一 child owner [#862](https://github.com/hamanpaul/paulsha-cortex/issues/862)，`work_id: recovery-registry-receipt`；父項 #497；[spec](../../specs/recovery-registry-receipt-spec.md)／[design](../../specs/recovery-registry-receipt-design.md)。Root 已接受原四件加 own OpenSpec 四件，共八份內容，status=accepted；本輪只更新接受狀態，尚未正式發布、註冊/binding 或產品執行授權。唯一未來 production 檔是 `paulsha_cortex/coordinator/registry.py`；產品 tests/runbook/changelog 是將來 Cortex 實作交付責任，本輪不建立或改動它們。

A→C→B→D2，D1→D2 是人工規劃骨架；A 只完成 registry AC，C/B/D 負責其餘 producer/consumer。舊 API None semantics、既有證據和母 S01–S13 均保留。root 2026-09-08 已裁決 OQ01/OQ02，確認八件 fresh review PASS 後正式接受內容。六份 consumer views 均應 accepted，但現行 accepted sizing 為8 Red；#831 的6 Yellow只作 loaded 後的條件投影，不能當派工依據。新增顯式 checkpoint 為 I09，原八組 AC 不刪，state=2 不降。現行算法對不完整材料反而降分，不得把 Yellow 數字當作 readiness。

## Repository Intake Prerequisites

**產品 run start 前**，由 root 指派的正式 repository-intake planning 責任方建立、審完、合併並發布自身 `openspec/changes/recovery-registry-receipt/{proposal.md,design.md,specs/**,tasks.md}` 與唯一 mapping；owner/source authority 只屬 `work_id: recovery-registry-receipt`。不能指望完整 accepted triad 的 fix-standard 產品 run 再進 propose 補件，它可能直接 plan gate→build。Intake 的合併/發布是 run 前的 planning 前置工作，不是 active 產品 tasks 的待完成 merge。

Own proposal/design 必須 self-contained，完整等價承載本 child spec/design 的規格、決策、邊界與 I01–I09，不以薄連結替代；specs/** 的 delta 與本 child 規格一致。Own tasks.md 與本 Todo 的 domain_breadth=0、state_consistency=2、invariant_count=9、artifact_classes=source/tests/documentation、Tasks/surfaces（source/tests/documentation/CLI）與適用 rules（R-09/R-16/R-19/R-22）一致，不能讓 first-plan 選取與 last-plan sizing 讀到不同契約。Intake 須對所有將被消費的 planning views 作等價/strict validate、唯一 child source binding 與 read-back，固定 exact refs/revisions；不借用 umbrella/B/C/D 的 change，衝突不得自行選一份忽略。

Operator/intake 已發布 baseline immutable；產品 candidate 對上述 frozen planning artifacts 只准既有 Tasks checkbox 的合法狀態切換，不改正文/metadata/0/2/9/owner/mapping、不新建另一套 proposal/design。缺產物或 binding 時退回正式 planning intake，禁止在產品 run start/freeze 後補造前置 authority。Own proposal/design/tasks 與 `specs/recovery-registry-receipt/spec.md` 內容已由 root 接受，唯一 child owner #862 已建立並讀回；尚未規劃 PR 合併發布或 mapping/binding。所有 pre-archive Tasks 未勾，T10 前置證據未齊，不得派工。

## Tasks

- [ ] **T01 source／I01/I07**：只在 registry.py 定義 additive versioned receipt/disposition/checkpoint 的 load/copy 契約；缺欄位不 backfill、不因 A 新增欄位寫回，未知/malformed 拒絕。保留既有 v1 migration/#501 repair 原政策與 fixtures，不宣稱所有歷史格式零寫入。
- [ ] **T02 source／I02**：實作 strict immutable recovery/checkpoint context、各自完整 payload digest 與全 registry 兩類 receipt 的 request_id collision 檢查；固定 encoding/golden vector，拒絕 caller mismatch、未知欄位、跨 target/種類重用、同 ID 更換 proof requirements，不能 hash actor/slice/action 當新 attempt ID。
- [ ] **T03 source／I03**：fresh slice revision、exact snapshot CAS、所有 registry 內有效 binding/state/repin writer 的 revision 更新與 ABA 守門；保留 update_slice(None) 語意。Legacy 舊 mutator 相容但不自動升級/不獲 ABA 保證；versioned row 依 D3 每個有效 mutation 計次。
- [ ] **T04 source／I04/I05**：建立 lookup／prepare／commit 原語；prepared 不開 pending、不清綁或冒充成功；complete 重送回原結果且零新增寫入。commit 同 snapshot revalidate、supersede 舊 B/R、明確清三欄、revision/history/receipt 一次落盤。
- [ ] **T05 source／I06**：提供 supersession／consumption 的 exact CAS、copy、冪等 persistence；required proof closure 只作內部資料契約，來源驗證與真 replacement/consume call sites 留 B/C，不把 stub 當實際 proof。
- [ ] **T06 tests／I01/I02/I07**：候選新增 tests/test_recovery_registry_receipt_497.py；覆蓋 legacy bytes/read/reload、未知 version、完整 golden request、ID/digest/target 負例、nested mutation copy、complete historical replay。測試檔尚未存在，不宣稱已 RED/GREEN。
- [ ] **T07 tests／I03/I04/I05/I08**：候選 tests/test_recovery_registry_receipt_497.py 增 exact pins/null、stale/new binding、A→B→A、identical repin、prepared/complete fresh registry、兩個舊 jobs、同 ID replay/衝突；比對 durable/memory、revision 與 history 次數。
- [ ] **T08 tests／I05/I06/I08**：fault injection 涵蓋 validation/persist/rename/fsync/rollback 與 incomplete proof；重用 tests/test_record_action_atomic_382.py、tests/test_workflow_registry.py、tests/test_coordinator_registry_headless.py 的相關回歸。rollback 自身失敗要報 fatal，不冒充 snapshot 已恢復。
- [ ] **T09 documentation／CLI／I08**：更新 registry recovery 契約/runbook，明列 B/C/D 的 allowed-action、control pins、done reconciliation 仍未接線；保留既有 CLI 無新增動詞。候選 wheel 從 checkout 外做 --help/read-only smoke，不呼叫 live manager；如需改第二 production module 必須停交 root，不偷塞 A。
- [ ] **T10 documentation／OpenSpec／changelog／S13**：核對 Repository Intake Prerequisites 已在產品 run start 前完成的 own 四件產物、唯一 owner/mapping、strict validation、正式 binding/read-back 與發布 revision 證據；產品 run 不負責建立/合併/發布這些前置產物，缺件即退回 intake，不補造 freeze 前 authority。Archive 前同步本 child changelog fragment、CHANGELOG.md 與本地驗收紀錄，核對 active tasks.md 只列 archive 前確實可完成的前置證據核對、source/tests、文件、本地驗證與 review；candidate 僅切換既有 checkbox，operator baseline 不變。本輪僅依 root 批准接受 #862 文件內容，沒有規劃 PR 合併發布/binding 證據、不改 CH，故本項仍未完成。
- [ ] **T11 tests／planning gates**：以 Superpowers 三件與 own OpenSpec proposal/design/tasks 六份真 accepted 內容逐件實跑 completeness，分組及合併重算 sizing，保留僅在 memory 改 draft 的負控制及歷史 draft/accepted-counterfactual 證據；確認兩個 plan 的 Tasks/0/2/9 相等，first-plan helper 與 last-plan sizing 不分歧，再對 #831 接受後算法作明示純投影。Negative control 保留 draft、注入 Open Questions、缺失/invalid dimension、fixture envelope 上限，不能因 OQ 已裁決而刪掉 blocker 負例。產品派工前重新評九組 invariant_count/artifact_classes/實際 qualification envelope，不固定 model/agent/effort、不以 unknown=0 續跑。
- [ ] **T12 source／I09**：只在 registry.py 提供 D7 顯式 checkpoint 原語；獨立 context/digest，完整 observed legacy row/binding/fingerprint/job refs/provenance 精確 revalidate；單 snapshot 新增 revision1＋checkpoint receipt，保留舊 binding/history/業務欄位，不變更 job disposition。不得由 read/prepare/commit/舊 mutator 呼叫，A 不認證 actor 或操作 live。
- [ ] **T13 tests／I02/I03/I05/I07/I08/I09**：新增完整 checkpoint golden bytes/fingerprint/request digest；真 malformed/缺 pins/偽造 fingerprint/完整 row 或 refs 漂移/未知 version/provenance 缺失/跨種類同 ID 負例；覆蓋 revision1 與 receipt 原子故障、restart replay、revision N 不回退、legacy 舊 mutator 不升級、checkpoint 前歷史 ABA 不冒稱可證。所有原 failure matrix 保留。
- [ ] **T14 documentation／CLI／I09**：明列 checkpoint 是本次明示觀測的新起點，非歷史 ABA 證明；C/B 後續 owner-bound action、proof 重驗與正式切換仍未接線。Checkpoint/recovery 必須不同 intent/ID，不在 A 偷新增 CLI 動詞、permission 或自動連帶 recovery。

## Post-archive Delivery Accounting

Active OpenSpec tasks 的 archive 前項目確實完成後，才由 Cortex 正式收尾流程封存 **本 child 的 `recovery-registry-receipt` change**；archive 動作本身不列成「archive 前必須已完成」的循環 checkbox。封存不是 merge/installed/live：exact-head merge、remote CI、installed runtime/live 驗收、C/B/D1/D2 接線及母 #497 S01–S13 closure 都以本段 prose／交付報告分帳追蹤，不搬入 active OpenSpec 的 archive 前 checkbox。這些待辦未完成不得宣稱已交付，也不得為 A 局部完成而 archive umbrella `fix-superseded-terminal-replay`、其他 child 或關 #497；本輪不執行 archive。

## Closed Engineering Decisions

- **OQ01 closed by root，2026-09-08**：採 spec I09/design D7 registry-only 顯式 checkpoint。Legacy 舊 mutator 相容、不自動升級；C/B 真 owner/proof/action 是後續責任，非 A 已實作或本輪 live 授權。
- **OQ02 closed by root，2026-09-08**：A additive 不觸發 read/reload 寫回；既有 v1/#501 migration 原政策/fixture 不變，不聲稱全面零寫回。

## Evidence

目前已完成 bounded source trace、八件內容接受與唯一 #862 owner 讀回，沒有 A 產品 code/tests、模型評測、registry 註冊/binding、規劃 PR 合併發布、commit/push 或 installed/live。實際 pure 函式、輸入與結果見 reports/review/refine-recovery-registry-20260907.md；現行 accepted8 Red，#8316 Yellow只是條件投影，產品派工仍須另經完整前置與資格 gates。
