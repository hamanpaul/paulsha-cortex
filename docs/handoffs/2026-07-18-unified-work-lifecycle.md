# Unified Work Lifecycle implementation handoff

日期：2026-07-18（Asia/Taipei）

## 停止邊界

本輪已把 unified lifecycle 的主要 runtime、Monitor read model、persona workflow 與 delivery gate 分階段合併至 `main`，並以 issue #31 跑到 live canary 的 `build` phase。因後續設計可能調整，工作刻意停在 `worktree-isolation` 已通過、`tdd-red` 尚未完成的邊界；沒有建立 canary PR、沒有 archive canary OpenSpec、沒有 merge 或宣告 done。

目前 `main` 為 `01431972a24aeb73f4e87967051787fd0c664b88`（PR #36 merge commit）。本 handoff 與 Manager 產生的三份 accepted planning artifacts 保存於 local branch `feature/14-unified-lifecycle-handoff`。

## 已落地範圍

- Monitor 已有跨 repo WorkItem source provider、durable last-good snapshot、confirmed/inferred correlation、四態 lifecycle reducer、degraded freeze、strict closure、list/get/explain socket 與 CLI read surface。
- Manager 已是 work mutation 與 workflow registry 的單一 writer；支援 manual/label claim、active-run resume、persona-preserving manifest、heterogeneous planning、disposable read-only planner、builder/verify/review phase spine 與 delivery gates。
- Registry 已升級 schema v2，保留 v1 legacy records；workflow step 保存 persona、executor/model/domain、inputs/outputs 與 evidence。
- Delivery automation 已有 exact-tree preflight、ForeignReview、current-HEAD GitHub review/check/thread gate、merge-commit與 CompletionRecord/remote closure 驗證。
- `agy`/Google 異質 brainstorm 已在 live run 成功，沒有使用 unsafe permission bypass 或全域 permission rule。
- README、CLI help、doctor、migration、ADR、glossary、OpenSpec 與 superpowers plan 已同步到目前實作。

主要已合併 PR：

- #17 / #18：persona/delivery foundation 與 Monitor truth model。
- #19：GitHub pagination 相容修正。
- #21 / #28 / #32：三次隔離 canary bootstrap。
- #22–#26、#29–#30：provider freshness、authority、resume、headless brainstorm 與 active-run continuity。
- #33：唯讀 workflow planner 的 disposable checkout trust gate。
- #34：failed planner sandbox 的 bounded retry reconciliation。
- #35：真實 Codex JSONL terminal evidence parser。
- #36：已合併 issue branch 的安全 builder worktree reuse。

## Live canary exact state

| 欄位 | 現值 |
|---|---|
| Issue | `hamanpaul/paulsha-cortex#31`，OPEN，已移除 `cortex:auto-on-going` |
| Work ID | `terminal-lifecycle-canary` |
| WorkflowRun | `workflow-ed15cd16ffa5e2c26306` |
| Lifecycle | `ongoing` |
| Current phase | `build` |
| Passed cards | claim、brainstorming、openspec-propose、writing-plans、worktree-isolation |
| Pending card | `tdd-red` |
| Builder job | `wf-3fa5c69448-tdd-red-5`，registry 仍為 `dispatched`，process 已停止，沒有 exit sentinel/evidence |
| Builder worktree | `$HOME/prj_pri/paulsha-cortex-worktrees/feature-31-terminal-lifecycle-canary` |
| Builder worktree state | clean；HEAD `01431972...`；local branch 相對舊 remote branch ahead 9 |
| Active OpenSpec | `terminal-lifecycle-canary`，0/3 tasks，未 archive |
| PR / merge | 無 |

`worktree-isolation` 的 canonical evidence 已綁定 candidate `01431972...`。停止 Manager 時，`tdd-red` 只建立了 Codex JSONL 開頭，沒有產生工作樹 diff、commit 或可接受 terminal evidence。

## Pause controls

為避免 active workflow 在沒有 auto label 時仍被 periodic resume：

- `cortex:auto-on-going` 已從 issue #31 移除。
- `cortex-manager.timer` 已 `disable --now`。
- `cortex-manager.service` 為 inactive。
- `cortex-monitor.service` 仍 active/enabled，read model 可繼續觀察。

不要在完成下方設計討論與 orphan job reconciliation 前重新 enable/start Manager。移除 auto label 按既有契約不會取消 active workflow，所以單獨加回 timer 會繼續推進。

## Durable planning 與 evidence

Manager 已產生並驗證下列 accepted artifacts，本 branch 將它們納入版本控制：

- `docs/superpowers/specs/terminal-lifecycle-canary-spec.md`
- `docs/superpowers/specs/terminal-lifecycle-canary-design.md`
- `docs/superpowers/plans/terminal-lifecycle-canary.md`

Runtime evidence 位於 operator state（不進 git）：

- Brainstorm：`$HOME/.agents/coordinator/evidence/planning/brainstorm-4ea700d6c243c60c603e1ae6a18d7c25.json`，SHA-256 `a859922a1ec8bf444473c27f61f158e558d7ee34f351d52165be2cdc272cc7ce`。
- Plan card：`$HOME/.agents/coordinator/evidence/workflow/f9dc691031a1ecc607547932910b0e670b44bdb2cc861f8046c992f760281cdb.json`，SHA-256 `7420b537385ce055f2d971882b2caeb4d88ca1155950e3c52c3ef2f98689ee51`。
- Worktree isolation：`$HOME/.agents/coordinator/evidence/workflow/15e13a900506bf59d355cb80ee80f8775dbf2a087a0df66fe370b814dcfeaece.json`，SHA-256 `cbd56d11eee097dcf1f0ee2a464dab4feeb7d04811b44ba21c3bd230e1c504ab`。
- Canonical registry：`$HOME/.agents/coordinator/jobs.json`。

## Canary history

- Issue #20 / `docs-only-lifecycle-canary`：揭露 systemd PATH、test isolation、resume 與 runtime routing 問題；沒有 candidate/PR/merge，issue 仍 OPEN，active OpenSpec 仍在。
- Issue #27 / `docs-only-lifecycle-canary-v2`：成功產生 brainstorm artifacts，之後揭露 planning source churn 與 Codex git trust 問題；沒有 candidate/PR/merge，issue 仍 OPEN，active OpenSpec 仍在。
- Issue #31 / `terminal-lifecycle-canary`：確認異質 brainstorm、same-run resume、planner retry、真實 terminal parser 與 builder branch reuse；目前停在 build boundary。

上述三個 canary 不得被宣稱完成；舊 active OpenSpec 與 issue 的清理方式要在設計討論後決定，不能直接偽造 archive/closure。

## 尚未解決、需要重新討論的設計

1. **Planning artifact handoff 到 builder**：brainstorm artifacts 原先發佈為 operator checkout 的 untracked files，builder worktree 由 committed `main` 建立，因此看不到 accepted plan。`worktree-isolation` agent 已觀察到 input glob 缺失但仍回 passed。應決定由 Manager 建立 planning commit、以 hash-bound copy seed worktree，或採其他 immutable handoff；目前只持久化 `workflow_inputs` 字串不足以證明 input 存在。
2. **Card prompt 契約太薄**：builder prompt 只有 phase/card/inputs/outputs/candidate，沒有 bounded source material、task-specific action、commit/test要求或 exact output schema說明。真實 agent 會自行搜尋 repo/skills，成本高且可能越過 card intent。
3. **Input gate**：terminalizer 驗 outputs/candidate，但 dispatch/terminalization 未機械驗證 declared input glob 已命中且 hash 與 planning authority一致。worktree-isolation 因此可在缺 plan 時 passed。
4. **Interrupted job reconciliation**：`wf-3fa5c69448-tdd-red-5` 為 `dispatched`、PID 已死、無 sentinel。必須透過 Manager/operator action 正式轉為 failed/needs_human 或 retry；不要直接修改 `jobs.json`。
5. **Self-review delivery seam**：使用者已明確要求由 maintainer exact-HEAD 自我審查以縮短迴圈，但 production ship validator 的 current-HEAD review authority仍以 Copilot gate 為主。若要支援 maintainer override，應是明確、可審計、repo/run scoped 的 evidence type，不能偽造 Copilot review或全域弱化 gate。
6. **Service/run-root 一致性**：service 使用 `PSC_RUN_ROOT=$HOME/.agents/run/cortex`，interactive CLI 預設 socket/run root 曾落到 `$HOME/.agents/run`；需統一 discovery 或 installer contract。
7. **Deployment observation scope**：`$HOME/.agents/config/paulsha/project-cortex.yaml` 目前為 canary 而只觀察 `paulsha-cortex`（大量 `ignore_dirs`）。何時恢復 fleet、是否先 read-only rollout，要重新確認。
8. **Terminal cleanup**：`unified-work-lifecycle` 尚有 30/33 tasks；issue #14、#12、#20、#27、#31 都仍 OPEN。只有真正完成的 task 可勾選，issue #12 不得因本批工作被虛假關閉。

## 建議的安全續作順序

1. 保持 Manager timer disabled，先讀本 handoff、三份 accepted planning artifacts、run/job registry 與 builder worktree。
2. 先決定 planning artifact handoff、input gate、prompt envelope 與 maintainer-review evidence 四個契約；必要時更新 ADR/OpenSpec/superpowers plan。
3. 用測試重現「accepted plan 只在 operator overlay、builder worktree 缺 input」以及「dead dispatched job 無 sentinel」；實作 fail-closed reconciliation。
4. 透過正式 Manager action處理 orphan `tdd-red` job，確認同一 run 繼續，不能另建 workflow 或直接編 registry。
5. 重新跑 `python3 -m policy_check --repo .`、`openspec validate --all` 與 configured CI-parity preflight。
6. 只有在 exact state 驗證後才重新 `systemctl --user enable --now cortex-manager.timer`；是否加回 issue #31 auto label另行決定，active run resume 本身不需要 label。
7. 完成 canary 前不得 archive、merge、close issue 或投影 done；完成後再處理舊 canary與 fleet Monitor restoration。

## 最近驗證基線

PR #36 exact HEAD `07d0ef4b1bac20934eafe6bee46024be5f6ef28f` 已做 maintainer self-review，GitHub 3 checks terminal-green、0 review threads；PR-context preflight為：policy 0 fail、OpenSpec 6/6、pytest `955 passed, 21 subtests passed`。這是 code baseline，不是 canary terminal completion evidence。
