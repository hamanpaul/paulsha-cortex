---
status: accepted
work_item: dispatch-decision-contract
domain_breadth: 1
state_consistency: 1
invariant_count: 8
artifact_classes:
  - source
  - tests
  - documentation
---

# 派工非 Job 決策契約工作清單

Issue：[Cortex #830](https://github.com/hamanpaul/paulsha-cortex/issues/830)。
Spec/design 為同 work_item 的 `dispatch-decision-contract-{spec,design}.md`。
accepted 只代表進件內容定案；當前 sizing=red 時不得整包派工。

## Tasks

- [ ] **T1 source/責任清冊**：從 runtime 精確 base 追 `dispatch_workflow_card`／`_dispatch_workflow_card` 到 daemon workflow-action、start/intake、manager resume、provider retry、terminal-next、periodic consumer，記錄函式/行號/回傳型別；不把 keyword 搜尋當全稱保證。
- [ ] **T2 tests/RED**：在 `tests/test_manager_daemon_intake_dispatch.py` 與新的 `tests/test_dispatch_decision_contract.py` 接入真 red producer 形狀，重現 KeyError；assert 零 Job/model invocation、run已存在、phase/facets/reason正確。模型與外部服務用可計數替身，fixture/state僅在tmp_path。
- [ ] **T3 source/最小修正**：在 coordinator 共享內部結果分類/投影契約，接完整 T1 清冊；真Job驗registry binding，合法decision保留reason與最新run，None/transition不造job_id；malformed/foreign-run payload fail-closed。
- [ ] **T4 tests/consumer矩陣**：涵蓋真正Job、None且未推進、確定性plan→build、needs-decomposition、runtime preflight refusal、plan-output missing、plan-review retry；start/intake重送與periodic resume不得新增run/Job、抹掉reason或誤轉resume-workflow-failed。
- [ ] **T5 tests/forced retry**：retry-build/retry-card的None、decision、transition仍拒絕假redispatched；補償維持needs_human與原失敗原因。正常replacement Job仍成功；reviewer independence、CAS、舊evidence/hash均不變。
- [ ] **T6 documentation/CLI help**：更新 `docs/unified-work-lifecycle.md` 的request完成/無Job決策/phase推進區別；同步必要README與CLI說明。以候選環境從checkout外真跑 `python3 -m paulsha_cortex.cli work start --help`、`run work --help` 與fixture-backed request JSON smoke，不呼叫live manager。CLI help未變也記錄實際輸出與exit code，不能只有字串mock。
- [ ] **T7 tests/CI**：先跑集中contract矩陣，再跑 `tests/test_dispatch_needs_decomposition_223.py`、`tests/test_manager_daemon_intake_dispatch.py`、`tests/test_coordinator_manager_daemon.py` 及T1定位的resume/provider/retry相關測試；最後 full pytest、既有CI/pinned preflight、含PR上下文policy、git diff --check。不可把單元測試等同live驗收。
- [ ] **T8 documentation/交付**：新增並commit `changelog.d/dispatch-decision-contract.md`、補 `CHANGELOG.md [Unreleased]`；PR保留正確issue/非目標關聯，獨立review與exact-head checks綠燈後由Cortex交付。新runtime重算sizing合格才選bounded canary，驗run/Job/decision/terminal分開呈現，保存installed/runtime版本和original frozen authority；不動其他進行中的工作。
