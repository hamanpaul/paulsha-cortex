---
status: accepted
work_item: sizing-stability-direction
domain_breadth: 0
state_consistency: 0
invariant_count: 7
artifact_classes:
  - source
  - tests
  - documentation
---

# Spec stability 方向修正工作清單

Issue：[Cortex #831](https://github.com/hamanpaul/paulsha-cortex/issues/831)。
Spec/design為同work_item的`sizing-stability-direction-{spec,design}.md`。

## Tasks

- [ ] **T1 documentation/rubric delta**：對照#208/#221與本spec R1–R7，在`docs/unified-work-lifecycle.md`記錄stability-risk-v2的0/1/2映射、unknown邊界、legacy/unversioned及明示新世代規則；不得偷偷更改歷史量表或舊紀錄。
- [ ] **T2 tests/RED**：於`tests/test_planning_sizing_score.py`先新增完整accepted→0、單缺kind→1、至少兩缺kind或marker/拒收artifact→2的規格oracle；保留舊算法會失敗的證據，再更新反向oracle。增`tests/test_sizing_stability_direction.py`做單調性/property、重複kind中不合格artifact、空artifact/unknown與非法report負例，所有分數由真函式算。
- [ ] **T3 source/純函式修正**：只在`paulsha_cortex/coordinator/planning.py`修改stability風險分量與算法識別常數；domain/state、acceptance_surfaces、orchestration、sizing band門檻、registry/schema/loader皆不改。不新增模型session或背景migration。
- [ ] **T4 tests/傳播與歷史**：在`tests/test_wiring_claim_time_sizing.py`、`tests/test_wiring_retry_sizing_recompute.py`與registry/completion測試建立隔離新舊fixtures；驗新claim/reclaim/正式retry重算採新映射，舊run/frozen revisions/CompletionRecord/evidence在read/reload後bytes/hash不變；缺少舊algo來源不被回填。record只用tmp_path，禁止碰live狀態。
- [ ] **T5 tests/邊界矩陣**：固定其餘四維，驗單模組與跨模組完整/缺漏/blocked、0..2每維、0..10總分、3/4與6/7 band邊界；缺/invalid domain/state在compute仍ValueError、current_sizing_snapshot仍(None,None)。新的低風險分不代表accepted/readiness gate被略過。
- [ ] **T6 documentation/CLI help與smoke**：更新README/操作說明中的新舊計分辨識與明示重新評估限制；從checkout外用候選Python環境真跑`python3 -m paulsha_cortex.cli work start --help`、`run work --help`，以隔離fixture跑唯讀stat/sizing JSON smoke並驗退出碼。無新增CLI旗標仍記錄CLI help實跑，不能只mock字串，也不接live服務。
- [ ] **T7 tests/CI及documentation/交付**：先跑本票兩份集中測試、`test_claim_sizing_band.py`、`test_wiring_claim_time_sizing.py`、`test_wiring_retry_sizing_recompute.py`、`test_registry_sizing_band.py`、`test_completion_sizing_band.py`，再full pytest、既有CI/pinned preflight與含PR上下文policy、git diff --check；新增並commit`changelog.d/sizing-stability-direction.md`並補`CHANGELOG.md [Unreleased]`。Cortex獨立review/exact-head交付後，記錄實際installed/runtime revision，再對#830重新計分；不修改其他active run。
