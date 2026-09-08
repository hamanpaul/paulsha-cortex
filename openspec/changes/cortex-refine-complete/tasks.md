## 1. B0 進件與責任邊界

- [x] 1.1 收斂十四類 plan、domain glossary、OpenSpec 與 PatchMUD producer issue 連結，獨立審查零未處置 MAJOR。（#832）
- [x] 1.2 將 Claude 已完成的進件驗證與本輪缺失複核併回 canonical child todo；保留 #828 獨立 ownership。（#832；operator原稿另存）
- [x] 1.3 保存 operator dirty 原稿並對齊實際 builder base；核對 Monitor confirmed authority 後只啟動一條低風險 Cortex canary。（#822 run workflow-97a9aa661e4816e38964；不是canary完成）
- [x] 1.4 進件 artifact-only commit/PR 經 preflight 與 CI；不關閉尚未實作的 issues。（#832 merge 60a3ffa8；#829仍OPEN）

## 2. B1 執行基礎

- [ ] 2.0 補 #830 非 Job 決策回覆與 #831 sizing stability 方向；保留合理單模組 sizing 及所有 gate；#833 獨立交付 Red planner 分解接線。
- [ ] 2.1 透過 Cortex 完成 #822 argv/YAML canary，取得 RED/GREEN、review、policy 與 terminal delivery 證據。
- [ ] 2.2 完成 #827 有界 refresh/coalesce、slow-provider 公平與 stop/diagnostics。
- [ ] 2.3 完成 #819 not-idle clock、periodic 有效 max_load/require_idle 與非法值處置。
- [ ] 2.4 依 accepted 三件組完成 #496 內容/狀態冪等，驗同 path 內容改變仍恰好記錄；保留現行 7/Red，#831 後真重評。
- [ ] 2.5 依 accepted 三件組分拆並完成 #497 原子解除綁定、持久 supersession 與 restart/late-terminal 回歸；#831 後仍預計 Red，#501 只核對已修及殘餘污染。
- [ ] 2.6 依 accepted 三件組完成 #821 no-op persistence、history retention、writer/fault/rollback 相容與安全暫存清理；完整 archive 缺口/截斷前備份保持列管。
- [ ] 2.7 完成 #823/#824 session 與 AGY timeout 合約，明示 cgroup restart survival 的獨立驗收。
- [ ] 2.8 完成 #825 最小 durable backoff，所有 lane 與 corrupt-state/expiry 可測；不標 R05 完成。
- [ ] 2.8.1 先依 #850 有界 store／immutable event-fold child 交付 component；C/D provenance、reconciliation 與所有 lane 接線仍由 #825 後續 child 承接。
- [ ] 2.9 以實際已載入 revision 驗 B1 成效，退出暫時 bypass 前保存 active jobs 與 rollback 方案。
- [ ] 2.10 由 #847 區分可信 frozen 自發布 metadata 等價與真 authority 變更；前者保持 needs_human/gates/attempt/model binding 且零 spawn，後者仍走合法 restart，缺 provenance 不豁免。

## 3. B2 可擴充配置與資格

- [ ] 3.1 為 R08/R09/R12 建立/重用精確 child work items；#835 已有 accepted profile schema/原生 effort/registry 相容三件組，完整母範圍仍 Red，先 schema-key child 再分拆其他入口，不先勾產品完成。
- [ ] 3.2 實作 adapter capability descriptors 與 conformance；新虛構 model/effort 無 central product-name diff。
- [ ] 3.3 實作 requested/resolved/observed profile 與可追溯 effective argv/config；unsupported pre-spawn fail-closed。
- [ ] 3.3.1 依 #849 交付純 schema／canonical key 核心，原生 effort 與 observed unknown 不補值；母 #835 routing／migration 與 #842 qualification 另留正式 evidence。
- [ ] 3.4 實作 PatchMUD versioned report consumer、legacy migration 與 profile/cohort/role/coverage 驗證。
- [ ] 3.5 由 #842 實作 qualification candidate→review receipt→approved roster 發布鏈；保留 operator 核可與 independence policy，不擴 #581 原scope。
- [ ] 3.6 實作安全 model register/probe 操作面與 TTL、角色擴充契約；探活不暗中消耗無上限額度。
- [ ] 3.7 定義 pool/account 非機敏 identity、instance authority 與所有權邊界，為共享 admission 建 fixture。

## 4. B3 遙測與預估

- [ ] 4.1 完成 #826 failure 原始訊號→持久化→消費端分類，保留 runtime-contract 硬阻擋。
- [ ] 4.2 整合 usage provenance：observed/estimated/unknown、增量/累計、input/cache/reasoning 不重複加總。
- [ ] 4.3 由 #836 核對各 provider 真正可用的 quota observation 介面，實作帶來源/TTL/window/unit 的 adapters 與 unknown fallback。
- [ ] 4.4 實作與 benchmark 分開的 operational track record，涵蓋失敗消耗、duration 及任務分布。
- [ ] 4.5 由 #837 實作 task/profile 用量 forecast 與冷啟動先驗、風險區間、版本與資料期間。
- [ ] 4.6 以 shadow 流程取得觀測覆蓋率、預估誤差與 confidence baseline；據此核定有限啟用門檻。

## 5. B4 動態派工與恢復

- [ ] 5.1 由 #839 完成 qualified feasible candidate selection，維持 explicit pin、permissions、role、independence 硬條件。
- [ ] 5.2 由 #838 完成多池/多時間窗原子 reservation 與同主機多 instance contention 測試。
- [ ] 5.3 完成 consumption reconciliation、crash/restart uncertain liveness 與 lease 不誤釋放。
- [ ] 5.4 完成 card/attempt 安全邊界的 fallback、supersession 與 artifact preservation；同耗盡 pool 不可繞過。
- [ ] 5.5 由 #843 補 R07 recovery matrix 與 exact-run/card CAS、late evidence、重送冪等、abandon owner-aware 資源處置；#497/#547/#577等原producer缺陷仍須修正。
- [ ] 5.6 通過計畫 12 個 quota/profile 場景，再有限 opt-in canary；保留 legacy policy 回復路徑與 receipts。
- [ ] 5.7 由 #844 完成 production stage reuse 的同run/claim-era安全cohort與可信採信；跨run新採信未支援需列管，不能以相同key改寫舊evidence。

## 6. B5 狀態與部署

- [ ] 6.1 核對 #828 獨立 producer 交付；由 #840 補 actual/planned/last、facets、quota wait 與 selection receipts 的 status 一致性，不接管原producer。
  - [x] 6.1.a [RED] #828 在 Cortex producer 邊界新增 execution-identity regression tests；以 registry job binding 驗證多卡、retry、缺值、未派工、跨 run/repo、needs_human、in-flight 與 completed status projection，保留 RED 供後續最小修正。
  - [x] 6.1.b [GREEN] #828 producer 以 registry job 的 run/repo/card/phase binding 投影 executor、model、job_id、card、identity_source 與 execution_state；planned、actual、last execution 與 unknown 不互相代填。
  - [x] 6.1.c [CONTRACT] #828 新增去識別化 status snapshot fixture 與 producer/consumer 欄位契約；僅完成 pre-archive handoff，consumer、pin、installed/runtime integration 與下游 issue closure 仍未完成。
- [ ] 6.2 由 #841 建立 CLI/site-packages/service loaded artifact/config identity 的同源驗證與 checkout 外 smoke。
- [ ] 6.3 完成 installer/doctor instance roots、writer ownership 與 owner-aware stop/cleanup 的契約驗收。
- [ ] 6.4 取得 upgrade/restart/rollback 對 active jobs 的實際 receipts；未重載程序不得標已部署。
- [ ] 6.5 PatchMUD #37 producer 交付後，以真實 report→approval→dispatch 驗跨 repo 接線；外部依賴未交付則保持未完成。

## 7. B6 整體閉環

- [ ] 7.1 以 #845 requirement delivery accounting 對全部 R01–R14 逐列核對 spec、work/run、測試、獨立 review、merge revision 與 runtime/installed evidence；索引建好不代表每條需求已交付。
- [ ] 7.2 逐 issue 重驗已修、取代與殘餘分類，補 closing/cross-reference；不依歷史清單批次猜測關閉。
- [ ] 7.3 完成 release/changelog/install 一致性與 plan ledger；全部必要驗收完成後才 archive 本 umbrella。
