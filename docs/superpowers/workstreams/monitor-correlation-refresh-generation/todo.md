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

唯一 parent owner 是 [paulsha-cortex issue 1064](https://github.com/hamanpaul/paulsha-cortex/issues/1064)，唯一 parent `work_item` 是 `monitor-correlation-refresh-generation`。Parent有七個refs，其中Superpowers spec/design/todo與own OpenSpec proposal/design/tasks六份規劃文件均以frontmatter帶此pair；OpenSpec capability delta spec由change directory綁定，不另加frontmatter。#1077、#1078各有不同issue與work_item、分開於下表規劃。正式runtime source binding尚未建立；這些文件仍為draft，沒有freeze或dispatch效力。

依賴次序：#1063 → #1077（durable attempt generation/failure marker）→ #1078（exact input/source + snapshot read-back + freshness API）→ #1064 producer umbrella completion → #1065 WorkAuthority consumer → #1054 pre-Builder run/claim reconciliation、first-Builder Manager gate、stale direct-resume stop與typed diagnostics。#1064不重做#1063、不吸收#1065 consumer或#1054 gate。

## Boundary

只規劃 `paulsha_cortex/monitor/work_api.py`、`work_snapshot.py`、`correlation.py` 的 attempt ledger orchestration、input/source revision capture、snapshot durable read-back與單一freshness API。未來測試集中於 `tests/test_monitor_correlation_refresh_generation.py`。不改 claim／Manager／CLI，不做live issue/run/snapshot操作、部署或產品implementation。

## Sizing and Gate Status

- 官方計算器：`paulsha_cortex.coordinator.work_bridge.current_sizing_snapshot()`，`fix-standard` combo；本bundle有七個binding refs，但 sizing helper 的 `_artifact_rows()` 實際輸入六份：Superpowers spec/design/todo 與本change OpenSpec proposal/design/tasks。OpenSpec capability delta spec 是規範payload，不另列為 sizing artifact。R-09/R-16/R-19由helper依repo process-level contract計入。
- Draft實算：`domain_breadth=1`、`state_consistency=2`、`acceptance_surfaces=2`、`spec_stability=2`、`orchestration=2`；`current_sizing_snapshot()`輸出 **9/10 Red**。
- 完整 accepted-status counterfactual：修正design的blocking open-question、proposal/task必需標題後，只在暫存副本把六份helper rows的frontmatter status改成accepted、其餘內容不變；官方completeness為complete、missing kinds為空，`spec_stability=0`，維度 `1+2+2+0+2`，總分 **7/10 Red**。這不是目前accepted狀態或派工資格。
- 因accepted projection仍Red，已查重並把producer拆成issue-backed slices。#1077與#1078各有三份同綁定Superpowers sizing rows，維度分別 `1+1+2+0+2`、各 **6/10 Yellow**（accepted-status counterfactual）；各自目前draft實算為 `1+1+2+2+2`、**8/10 Red**。此切片只是規劃結果，仍須各自獨立接受後才符合intake條件。
- Parent `monitor-correlation-refresh-generation` work item只描述整體能力與issue依賴，不取代兩個child work items。#1063→#1077→#1078→#1064→#1065→#1054是硬依賴順序；此任務不執行intake。
- OpenSpec使用official `spec-driven` change `monitor-correlation-refresh-generation`；`openspec validate monitor-correlation-refresh-generation --strict --no-interactive`與repo-wide `openspec validate --specs --no-interactive`須在PR提交前實跑。
- 本repo PR-context policy需帶實際title/body/labels/base/head執行；本PR引用issue但不應在規劃文件合併時關閉產品issue，故使用白名單 `policy-exempt:issue-link` 並附理由。

## Tasks

### Issue-backed producer slices

| Issue / work item | Owner and bounded acceptance | Dependency |
|---|---|---|
| [#1077](https://github.com/hamanpaul/paulsha-cortex/issues/1077) / `monitor-refresh-attempt-ledger` | Monitor attempt ledger: durable monotonic generation, scan-before `running` marker, failure/degraded marker, restart/unknown/I/O fail-closed; does not publish trusted success | #1063 |
| [#1078](https://github.com/hamanpaul/paulsha-cortex/issues/1078) / `monitor-trusted-freshness` | Exact parsed input/source revision, snapshot durable read-back, same-generation success marker, one read-only freshness API with typed fail-closed results | #1077 (transitively #1063) |

完成兩個implementation slices後才滿足#1064 umbrella producer acceptance；#1065/#1054仍各自保有consumer與Manager gate。

- [ ] **T1 / R1-R2 — #1077 attempt marker model/store**：在snapshot旁新增versioned sidecar marker；嚴格解析unknown/malformed；generation從durable latest單調加一；scan前write `running`；失敗／degraded outcome durable-write。成功狀態等#1078驗證input/source與snapshot read-back後才可發布；crash/storage failure保持fail closed。
- [ ] **T2 / R3 — #1078 exact correlation input revisions**：讓`correlate_work_sources()`回傳同次parse所讀的`.cortex/work-items.yaml` raw revision；缺檔使用fixed absent sentinel；收集repo相關provider revision與sorted `source_id → revision`；unknown/degraded inputs不能成功。
- [ ] **T3 / R4 — #1078 durable snapshot read-back**：完整candidate成功時先寫WorkSnapshot，從durable path reload；核對canonical digest、sequence、repo/work rows、ownership與本次attempt擷取的source/provider revisions；read-back相符才以同一generation寫succeeded marker並綁定sequence/digest。exception/degraded保留last-good診斷payload但不標成功。
- [ ] **T4 / R5-R6 — #1078 single trusted freshness API**：在`WorkModelRefresher`使用最近service refresh傳入的canonical `ProjectState` root mapping重讀current override；缺少／重複／未知root即untrusted。核對latest per-repo attempt、generation、snapshot digest/sequence、input revision、source revision集合、必要provider freshness與`stale_after_seconds`；回傳typed trusted/reason evidence；missing/legacy/unknown/stale/mismatch一律untrusted，沒有last-good fallback。
- [ ] **T5 / R1-R8 — Deterministic fixture matrix**：使用tmp repo、injected stores/fake providers/fake clock，測初始成功與matching row後refresh failure、provider degraded保留last-good、override已變但未refresh、新override成功後generation前進、unknown/missing marker、超齡、混代source、snapshot read-back drift、marker write failure與restart running marker；逐例檢查durable bytes/API result。
- [ ] **T6 / R7 — Dependency/ownership integration**：以#1063產出的canonical qualification/path source revisions作fixture，不複製其guard；驗證freshness API自身不改WorkAuthority、claim、Manager registry、Todo qualification或override。
- [ ] **T7 / R8 — Documentation and CLI surface**：更新Monitor freshness/API文件，說明legacy讀取相容、unknown marker fail closed及diagnostic reason；本票不新增CLI/API command，若實作期間擴大CLI輸出則同步CLI help與README並重新sizing。
- [ ] **T8 / delivery — Changelog and gates**：新增`changelog.d/monitor-correlation-refresh-generation.md`與`CHANGELOG.md [Unreleased]`；focused tests後跑full pytest、strict OpenSpec、pinned CI/preflight、實際PR-context policy與`git diff --check`；留下精確結尾。不得以本planning PR測試或文件驗收宣稱產品AC通過。
- [ ] **T9 / dependencies — Later consumers**：先完成#1063→#1077→#1078→#1064 producer umbrella；之後#1065實作WorkAuthority freshness consumption；#1054再由其owner獨立實作Manager admission與zero-side-effect diagnostics。這些不是本票可勾的交付。

## Evidence and Delivery State

目前source baseline為`origin/main` `6a32a3e5e0af841794f340313c11f60f2999f6ae`；live issues於2026-09-25讀取，均open、無comments。這是draft planning PR；無產品code、測試執行、live binding、Cortex intake、issue closure、merge、部署或runtime證據。產品tasks全未完成。
