---
work_item: unified-work-lifecycle
status: accepted
issue: hamanpaul/paulsha-cortex#14
---

# Unified Work Lifecycle Implementation Plan

> **Execution contract:** 依phase在`wt/unified-work-lifecycle/<subtask>` worktree逐Task執行；每個behavior先取得指定RED，再做最小實作。每個code PR更新changelog、勾OpenSpec tasks並跑full gates。此文件是exact execution boundary；`openspec/.../tasks.md`只追蹤apply進度。

**Goal:** 建立跨repo Work Item read model與Manager single-writer persona/delivery workflow，只有remote strict closure成立才done。

**Architecture:** Monitor providers先產versioned sources，durable last-good store保存每provider成功資料，correlator分confirmed/inferred，reducer投影四態。Manager registry v2保存WorkflowRun/Step與claim key，依persona manifest執行planner/build/review/ship。CLI與socket共用`cortex-work/v1` serializer。

**Source spec:** `docs/superpowers/specs/2026-07-17-unified-work-lifecycle.md`

## Cross-task invariants

1. `.cortex/work-items.yaml`、`work_item`、snapshot與JSON schema不得另取別名。
2. Provider failure保留last-good；不能remove/done/dispatch/merge。
3. Inferred association永不進mutation authority。
4. Manager是workflow/GitHub mutation唯一writer；read CLI不寫registry。
5. Brainstorm、ForeignReview、Copilot三gate不可互換。
6. 每次push使Copilot epoch失效；merge前後都重讀remote facts。

---

## Task 0: Baseline與planning artifacts

**Files:** `CONTEXT.md`、`docs/adr/0001-unified-work-lifecycle-authority.md`、本spec/plan、`openspec/changes/unified-work-lifecycle/**`、issue #4相關既有Monitor files/tests、stale active change paths。

1. 核對issue #14與related issue，不建立重複umbrella。
2. 完成並validate本change。
3. 依issue #4先RED transient unavailable/invalid interval，再最小修正scan health。
4. 刪除已正式archive的`dispatch-discipline-improve` active duplicate，確認canonical specs保留。
5. 以PR #11、default branch archive與tests證據核對issue #10；只有剩stale artifact時才關閉。

Run:

```bash
python3 -m pytest -q tests/test_monitor_scan_health.py tests/test_stage9_project_monitor.py tests/test_stage9_project_monitor_service.py
openspec status --change unified-work-lifecycle --json
openspec validate unified-work-lifecycle --type change --strict
```

---

## Task 1: PR A — Provider與durable snapshot foundation

**Create:** `paulsha_cortex/monitor/work_models.py`、`providers.py`、`work_snapshot.py`、`tests/test_monitor_work_providers.py`、`tests/test_monitor_work_snapshot.py`。

**Modify:** `paulsha_cortex/config/paths.py`、`monitor/config.py`、`monitor/service.py`、`monitor/snapshot.py`、`tests/test_monitor_scan_health.py`、`CHANGELOG.md`。

1. RED-test fixed globs、archive exclusion、GitHub seam auth/rate-limit/timeout、local overlay revision與active/archive collision。
2. RED-testdefault path `$PSC_AGENTS_ROOT/monitor/work-items.snapshot.json`、`PSC_MONITOR_STATE_ROOT` override、0600、atomic crash、unknown schema與restart bootstrap。
3. Implement typed WorkSource/ProviderSnapshot/WorkItem serializers；GitHub命令使用typed argv/JSON，不經shell。
4. Implement per-provider success replace/failure retain；900s freshness只產gate，不刪read data。

Focused PASS:

```bash
python3 -m pytest -q tests/test_monitor_work_providers.py tests/test_monitor_work_snapshot.py tests/test_monitor_scan_health.py
```

## Task 2: PR A — Correlation、reducer與read APIs

**Create:** `paulsha_cortex/monitor/correlation.py`、`lifecycle.py`、`work_api.py`、`tests/test_monitor_work_correlation.py`、`tests/test_monitor_work_lifecycle.py`、`tests/test_work_cli.py`。

**Modify:** `paulsha_cortex/monitor/server.py`、`monitor/service.py`、`paulsha_cortex/cli.py`、`coordinator/cli.py`、既有socket/help tests、README、CHANGELOG。

1. RED-test override strict schema/path escape/collision、frontmatter scalar key、two-signal inferred、competitor、unlink exclusion與restart。
2. RED-test reducer priority、four artifacts、partial closure、degraded freeze、issue reopen/OpenSpec reactivate。
3. RED-test `cortex-work/v1` exact envelope/ordering/on-going spelling/list filters/explain。
4. Implement read-only `list/work show`與socket list/get/explain/subscription；舊ProjectState保持可用並標deprecated。
5. Run all Monitor/CLI/help tests，更新OpenSpec Task 1。

PR A gate:

```bash
python3 -m pytest tests/ -q
openspec validate --all --strict
python3 -m policy_check --repo .
git diff --check
```

---

## Task 3: PR B — Registry v2與persona manifest

**Create:** `paulsha_cortex/coordinator/workflow.py`、`workflow_registry.py`、registry migration fixtures/tests。

**Modify:** `coordinator/registry.py`、`manager.py`、`manager_daemon.py`、`control/{contract,client}.py`、`deck/{schema,compile}.py`、cards/combos、`persona/personas.yaml`、tests、CHANGELOG。

1. RED-testv1 immutable backup、v2 atomic write、legacy records no association、malformed rollback、claim-key duplicate/restart。
2. RED-testWorkflowRun/Step fields與transition；all mutations through control queue。
3. RED-testDeck每step保留card persona binding，禁止global builder覆蓋；加入planner與`feature-oneshot`。
4. Implement minimum schema/migration/manifest wiring；不在本Task啟用auto claim/ship。

Focused PASS:

```bash
python3 -m pytest -q tests/test_workflow_registry.py tests/test_deck_schema.py tests/test_deck_compile.py tests/test_control_contract.py tests/test_control_client.py
```

## Task 4: PR B — Agy與異質completeness gate

**Create:** `paulsha_cortex/coordinator/planning.py`、`model_identities.py`、agy launcher tests/fixtures、packaged `model-identities.yaml`。

**Modify:** `coordinator/launcher.py`、`manager.py`、package data、persona/deck tests、CHANGELOG。

1. RED-testmissing accepted spec/design/plan、line-level blocking marker、inline/fenced-code false positive、question pack、secondary evidence-only與primary integration。
2. RED-testselection order、primary-domain exclusion、same/unknown domain與malformed output。
3. RED-testagy argv含print/plan/sandbox且沒有unsafe bypass；capability probe failure不視為available。
4. Implement planner gate與immutable evidence；brainstorm evidence不能填ForeignReview ref。
5. Run PR B full gates與independent review，更新OpenSpec Task 2。

---

## Task 5: PR C — Claim與work mutations

**Create:** `paulsha_cortex/coordinator/claim.py`、work action tests、GitHub label seam。

**Modify:** work CLI/API、control contract/client、manager/daemon、workflow registry、CHANGELOG。

1. RED-testmanual default、confirmed Todo start、Todo+issue+label auto、issue-only no dispatch、missing issue、label removal、provider stale與restart idempotency。
2. RED-testlink/unlink原子override與exclusion；`auto --enable/disable`使用REST；CLI不直接寫workflow registry。
3. Implement `show/link/unlink/start/resume/auto` control requests與next actions。

## Task 6: PR C — Preflight與GitHub review epochs

**Create:** `paulsha_cortex/coordinator/preflight.py`、`github_delivery.py`、`delivery.py`及seam tests。

**Modify:** manager/workflow/completion/CLI、package help、CHANGELOG。

1. RED-testofficial archive/tasks/spec/doc/changelog gate與zh-TW PR metadata/closing keyword。
2. RED-testquick policy、`PSC_PREFLIGHT_CMD` initial metadata/`--pr N`、required suite、exact-tree skip-tests。
3. RED-testchecks terminal states、old-HEAD/error Copilot、unresolved/outdated threads、push invalidation與HEAD race。
4. Implement per-HEAD epoch；request `@copilot` every push；兩輪/15min budget，逾限needs_human。

## Task 7: PR C — Merge與remote closure

1. RED-test final reread race、mergeability/check/thread/closing/archive blockers、禁止`--auto`。
2. RED-testmerge後ancestry、issue close、remote active removal/archive presence、Todo completion與CompletionRecord hashes。
3. Implement `gh pr merge --merge`與post-merge fetch verification；任何partial fact保持ongoing。
4. Run PR C full/integration gates、review，更新OpenSpec Task 3。

---

## Task 8: PR D — Doctor、docs、service migration

**Create:** doctor module/tests與migration docs。

**Modify:** top-level CLI/help snapshots、service installer/templates、README、usage、package data、CHANGELOG。

1. RED-test`doctor --probe-live` gh auth/permissions、label、preflight executable、identity、agy smoke、state/socket/service paths。
2. Implement probes with secret-safe diagnostics；不得輸出token或完整credential env。
3. Document override/frontmatter/snapshot/registry migration、manual/auto workflow與recovery。

## Task 9: PR D — Canary與archive

1. 建低風險docs-only issue並加auto label；刻意缺accepted plan。
2. 觀測異質brainstorm→builder→ForeignReview→official archive→preflight→Copilot→merge commit→remote closure→done。
3. Failure時停止fleet auto rollout，保存evidence並修正/re-review；不得換reviewer繞gate。
4. Canary通過後執行`openspec archive -y unified-work-lifecycle`，只更新issue #12實際完成項。
5. Run final full integration、policy、preflight與current-HEAD review。

---

## Per-PR completion gate

```bash
python3 -m pytest tests/ -q
openspec validate --all --strict
python3 -m policy_check --repo .
git diff --check
${PSC_PREFLIGHT_CMD}  # 依該工具contract傳PR metadata或--pr N
```

每個fix後必須re-review。只有all gates對final tree有fresh evidence才可宣告該PR ready；不得在未明確授權時push、開PR或merge。
