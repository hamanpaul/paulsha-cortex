---
status: draft
work_item: monitor-correlation-refresh-generation
issue: 1064
domain_breadth: 1
state_consistency: 2
invariant_count: 8
artifact_classes:
  - source
  - tests
  - documentation
---

# Monitor correlation refresh generation 工作計畫

## Binding

唯一 owner 是 [paulsha-cortex issue 1064](https://github.com/hamanpaul/paulsha-cortex/issues/1064)，唯一 `work_item` 是 `monitor-correlation-refresh-generation`。Spec、design、todo與 own OpenSpec proposal/design/spec/tasks共七個ref均重複此pair；正式 runtime source binding尚未建立。此文件仍為 draft，沒有 freeze或dispatch效力。

依賴次序：#1063 先交付canonical Todo qualification與existing-path admission contract；#1064 以其source/correlation輸入為前置，交付Monitor generation/freshness producer；#1065 再接WorkAuthority reader；#1054 最後實作pre-Builder run/claim reconciliation、first-Builder Manager gate、stale direct-resume stop與typed diagnostics。#1064 不重做#1063、不吸收#1065 consumer或#1054 gate。

## Boundary

只規劃 `paulsha_cortex/monitor/work_api.py`、`work_snapshot.py`、`correlation.py` 的 attempt ledger orchestration、input/source revision capture、snapshot durable read-back與單一freshness API。未來測試集中於 `tests/test_monitor_correlation_refresh_generation.py`。不改 claim／Manager／CLI，不做live issue/run/snapshot操作、部署或產品implementation。

## Sizing and Gate Status

- 官方計算器：`paulsha_cortex.coordinator.work_bridge.current_sizing_snapshot()`，`fix-standard` combo；artifact rows為本todo、同work_item Superpowers spec/design與本change OpenSpec proposal/design/spec/tasks。R-09/R-16/R-19由helper依repo process-level contract計入。
- Draft實算：`domain_breadth=1`（三個同一Monitor domain的production modules）、`state_consistency=2`（monotonic attempt/outcome、snapshot marker ordering/read-back）、`acceptance_surfaces=2`、`spec_stability=2`（planning views尚未accepted）、`orchestration=2`；總分 **9/10 Red**。此分是本PR當下draft輸入的實際helper結果，不是accepted sizing或派工資格。
- Intake前需獨立接受七份同綁定planning views、再用 current helper重算；若仍Red，按正式流程真拆分，不能以status或宣告值調低分數。此任務不執行intake。
- OpenSpec使用官方 `spec-driven` change `monitor-correlation-refresh-generation`；提交前須以 `openspec validate monitor-correlation-refresh-generation --strict --no-interactive` 實跑。
- 本repo PR-context policy需帶實際title/body/labels/base/head執行；本PR引用issue但不應在規劃文件合併時關閉產品issue，故使用白名單 `policy-exempt:issue-link` 並附理由。

## Tasks

- [ ] **T1 / R1-R2 — Attempt marker model/store**：在snapshot旁新增versioned sidecar marker；嚴格解析unknown/malformed；generation從durable latest單調加一；先write `running`後才refresh；terminal success/failure分開保存；storage failure/crash保持fail closed。
- [ ] **T2 / R3 — Exact correlation input revisions**：讓`correlate_work_sources()`回傳同次parse所讀的`.cortex/work-items.yaml` raw revision；缺檔使用fixed absent sentinel；收集repo相關provider revision與sorted `source_id → revision`；unknown/degraded inputs不能成功。
- [ ] **T3 / R4 — Durable snapshot read-back**：完整candidate成功時先寫WorkSnapshot，從durable path reload；核對canonical digest、sequence、repo/work rows、ownership與本次attempt擷取的source/provider revisions；marker以同一generation連結current input revision與snapshot digest。read-back相符才把marker轉為succeeded並綁定該sequence/digest。exception/degraded保留last-good診斷payload但不標成功。
- [ ] **T4 / R5-R6 — Single trusted freshness API**：在`WorkModelRefresher`使用最近service refresh傳入的canonical `ProjectState` root mapping重讀current override；缺少／重複／未知root即untrusted。核對latest per-repo attempt、generation、snapshot digest/sequence、input revision、source revision集合、必要provider freshness與`stale_after_seconds`；回傳typed trusted/reason evidence；missing/legacy/unknown/stale/mismatch一律untrusted，沒有last-good fallback。
- [ ] **T5 / R1-R8 — Deterministic fixture matrix**：使用tmp repo、injected stores/fake providers/fake clock，測初始成功與matching row後refresh failure、provider degraded保留last-good、override已變但未refresh、新override成功後generation前進、unknown/missing marker、超齡、混代source、snapshot read-back drift、marker write failure與restart running marker；逐例檢查durable bytes/API result。
- [ ] **T6 / R7 — Dependency/ownership integration**：以#1063產出的canonical qualification/path source revisions作fixture，不複製其guard；驗證freshness API自身不改WorkAuthority、claim、Manager registry、Todo qualification或override。
- [ ] **T7 / R8 — Documentation and CLI surface**：更新Monitor freshness/API文件，說明legacy讀取相容、unknown marker fail closed及diagnostic reason；本票不新增CLI/API command，若實作期間擴大CLI輸出則同步CLI help與README並重新sizing。
- [ ] **T8 / delivery — Changelog and gates**：新增`changelog.d/monitor-correlation-refresh-generation.md`與`CHANGELOG.md [Unreleased]`；focused tests後跑full pytest、strict OpenSpec、pinned CI/preflight、實際PR-context policy與`git diff --check`；留下精確結尾。不得以本planning PR測試或文件驗收宣稱產品AC通過。
- [ ] **T9 / dependencies — Later consumers**：#1065在此API合併後實作WorkAuthority freshness consumption；#1054待#1063/#1064/#1065完成後由其owner獨立實作Manager admission與zero-side-effect diagnostics。這些不是本票可勾的交付。

## Evidence and Delivery State

目前source baseline為`origin/main` `6a32a3e5e0af841794f340313c11f60f2999f6ae`；live issues於2026-09-25讀取，均open、無comments。這是draft planning PR；無產品code、測試執行、live binding、Cortex intake、issue closure、merge、部署或runtime證據。產品tasks全未完成。
