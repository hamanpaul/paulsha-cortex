# Evidence／profile intake 整合核對

本次是規劃文件交付，不是產品修正；產品只經 Cortex workflow 實作。
Root 的整合範圍包含四組 accepted 三件組、作者報告、本文、`.cortex` links、
plan/OpenSpec ledger 與 changelog，與兩個隔離 authoring 子任務的原 scope 分開。

## Scope 與驗證

- Root 全文讀 #496/#497/#821 九件、#835 初稿與最終 legacy 負例；另保留兩份作者 trace。
- 獨立 reviewer 以 live 基底 `79ba6447` 核對 source/tests、#832 原 AC 與 ownership；
  evidence 三件 PASS，原 10/13/11 項 Tasks 保留；profile 最終版另經獨立 review PASS。
- #496 真分數 7、#497 9、#821 8、#835 10，均 Red；#831 後的 5/7/6/8
  僅保持其他輸入不變的推算。需實際 loaded runtime 重評，不能提前宣稱 Yellow。
- completeness／contract pure checks 通過不等於 model qualification；envelope unavailable
  的 bypass 仍不是能力證據。任何未落地測試、PR/merge、installed/live 均不得勾完成。
- Root 另從 checkout 外以現行 runtime 真正 `load_cards/load_combo`、completeness、
  `compute_sizing_score` 與 `plan_review_gate` 重跑四件：7/9/8/10，
  source/tests/documentation/CLI 與 R-09/R-16/R-19/R-22 全通過；
  四件移除 Tasks 標題的記憶體負控制均被拒，未寫 registry 或執行模型。
- 原始 source baseline、helper 參數、負控制與作者當時未提交的操作紀錄見
  [evidence 報告](refine-evidence-intake-20260907.md)及
  [profile 報告](refine-profile-intake-20260907.md)，不是對整合後 Git 狀態的宣稱。
- 獨立 reader 對本輪文件的五題重讀 PASS：能區分規劃/產品、現行/條件式 sizing、
  原子性/retention residual、原生 effort/legacy unknown 與跨 repo qualification DAG。
  此為 reader test，不冒充產品故障測試或 live 驗收。

## 不可省略的界線

1. #496 no-op 必須先驗內容及完整状态；不建立 record_action/update_slice 的新跨寫入交易。
2. #497 superseded/consumed 分開；必要 proof durable 後才 consumed，crash 要 reconcile；
   原子解除綁定不把 filesystem reclaim 冒稱同一筆交易，也不偷接管 #818 多 writer。
3. #821 only byte-equal no-op、true append fixture、digest rollback、hardlink fallback；
   unknown writer 的老 tmp 仍略過。輪替非全 archive，需要完整歷史的部署先保留原 registry 備份。
4. #835 不設固定模型/agent/effort 名單；新增協定允許受控 adapter，既有協定新增模型/原生 effort 為資料擴充。
   unknown observed/policy 不猜填；歷史 chain 不回寫，正式新 authority 才可切新版。
5. PatchMUD #37 是外部 CLI/file producer，#581 保留五項既有 scope，#842 擁有 qualification
   publication/receipt/CAS/revocation。schema-key child 先行，避免 #835/#842 母票相互等待。
6. #497/#835 修正 stability 後仍 Red，人工 child 只是候選，仍需獨立審查/凍結；
   #833 自動分解未實作，母票仍持有整合與 closeout，不能以 child 完成取代全母驗收。

## Delivery gate

此 planning PR 須通過 strict OpenSpec、完整 preflight、exact-head CI 與 review；
policy-exempt:issue-link 用於不誤關尚未完成的產品票。
現場 #822 另以原候選進行真 verification，不因這批 isolated authoring 改寫 frozen inputs。
本輪另外列管的 [#847](https://github.com/hamanpaul/paulsha-cortex/issues/847) 是獨立產品 child，
root 已全文讀回 10 項 AC；只完成開票，未把 metadata 等價 helper 或 live guard 寫成已實作。
