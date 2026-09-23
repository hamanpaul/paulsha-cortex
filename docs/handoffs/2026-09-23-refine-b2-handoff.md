# Refine B2 進度 handoff（#819／#946／#849 落地 → #862／#948 在飛）

日期：2026-09-23（Asia/Taipei）
機器：9900X（WSL2）
前一份：`docs/handoffs/2026-09-21-refine-b2-handoff.md`
接手者：Codex（本文件即為交接指令來源；§1 的角色邊界優先於其餘建議）

## 1. 角色邊界（沿用 09-17／09-21 使用者裁決，未變）

- operator 只做四件事：**進件**（註冊 work item＋accepted 三件套）、**intake**、**needs_human 裁決**、**ship 卡死時的場外交付**。
- 不在 job 內修平台；平台缺陷開票，必要時另外進件派工。
- 只 focus `cortex` instance；`hippo`／`conventions` instance 不動。
- 每條 run 要走到 issue closed，run 以 `retire-delivered`／`abandon` 收乾淨。

## 2. 本輪落地（2026-09-21 夜 → 09-23）

| 票 | 交付 | 方式 | 介入 |
|---|---|---|---|
| #819 daemon tick 時鐘／idle | PR #947（main `5450c053`） | `workflow-e75b36c500fb15fde025` | **maintainer 路徑首次自動 merge**：`review-attest`→`resume`→Manager 自己 merge＋`Closes` 關 issue；下一 tick 卡 `remote closure blocked: todo-incomplete`（#810）→ `retire-delivered` |
| #946 backoff 對帳重播 | PR #951（main `6c7d6441`） | `workflow-71531694648c5b513dea` | 採 5 條 Copilot finding→attest→resume→Manager merge；之後卡 `delivery requires one canonical verify evidence job` → `retire-delivered` |
| #849 execution-profile schema core | PR #949（main `5906e723`） | `workflow-3960761d0991a70f476e` | 場外：解 CHANGELOG 衝突（#943）、採 1 條 nit、駁 1 條、`Architecture HTML` check 重跑即綠（flaky）→ merge → `retire-delivered` |
| 進件 docs PR | #950（#946／#948 三件套＋work items） | operator | policy_check 帶 PR 上下文 0 fail |

`main` = `5906e723`；open issue 30 張（09-21 是 96，期間你另做過清理）。

## 3. 在飛的兩條 run（接手第一優先）

### 3.1 #862 `recovery-registry-receipt`（run `workflow-9dc654fef3850cc68deb`）

- 候選 `fac52740`（verify＋code-review 已過）、PR **#954**（`CONFLICTING`／CHANGELOG，見 §5.2）。
- 現在在 build：operator 已 `retry-build`（copilot）要求採納 Copilot 四條 review：
  1. `docs/evidence/recovery-registry-receipt-local-validation.md` 內嵌 `$HOME/prj_pri/paulshaclaw/.venv/...`（R-21 tier 風險）→ 換成 `$VENV_PYTHON`／`python` 佔位。
  2. `openspec/specs/recovery-registry-receipt/spec.md` 仍是 archive 佔位 `TBD - created by archiving change …`（`paulsha_cortex/openspec_archive.py` 產生）→ 補真 Purpose，比照 `openspec/specs/agy-builder-support/spec.md`。
  3. `registry.py` 的 `_maybe_bump_binding_revision`（約 2606 行）**無任何呼叫點**（operator grep 確認）→ 接線或刪除。
  4. binding revision 與 `record_job_consumption()`／`record_job_supersession()` 可能不同步 → 要求 builder 驗證後修或附證據反駁。
- **ruling #4（重要，已寫進派工 reason）**：branch 上的 `chore(openspec): archive …` commit 是 **Manager 自己的 `openspec-archive` 卡**（job `wf-c66c74cbe7-openspec-archive-829`，executor `cortex-manager`）建的，不是 builder 違規；不要回退。詳見 §5.1 與 #953。
- 落地後：解 CHANGELOG 衝突 → 等 checks → `review-attest`＋`resume`（in-lane）或場外 merge → `retire-delivered`。

### 3.2 #948 `copilot-review-adopt-existing`（run `workflow-5fb6da3bc6d2cfc3c3ed`）

- 候選 `76ea5047`（verify 已過）、PR **#952**。code-review 判 `blocking-findings`（1 條 minor）：`tests/conftest.py` 加了 sys.path 重排（讓 worktree 套件贏過 runtime pin），超出「change nothing else」的裁決範圍，reviewer 要求 operator 確認。
- operator 複驗：該改動只影響 pytest import 解析、CI 內為 no-op，且正是本批一直撞的「daemon 跑 pin、測試卻該驗 checkout」這類問題的防護。**建議接受**，做法：`review-attest`（summary 寫明接受理由）→ `resume`；已 `resume` 一次，Manager 重派 code-review（job 851），若再判 rejected 就走 attest。
- 本票內容（採信既有 exact-HEAD Copilot review）**正是解掉 §5.2 那個每條 run 都會撞的 `copilot-review-timeout`**，優先讓它落地。

## 4. Runtime 現況

- pin：`~/.agents/runtime-pins/cortex-442fe23f-20260918a`（**不是** main）。`#946` 已 merge，但升 pin 前必須：以新 pin 的 `executor_backoff` API 對真 coordinator root 試算四個 identity 的 admission（#946 的教訓，見 `pin-a8323abd-rollback-946` 記憶），確認不再有 `executor-backoff-unknown`；`in_flight` 為 0 時才重啟（#582）。
- executor 可用性：
  - **codex `gpt-5.6-luna`：額度用盡，2026-09-28 20:00 才恢復**（job log `turn.failed`：`try again at Sep 28th, 2026 8:00 PM`；`provider_outcome.reset_at` 仍是 `None`——文字 reset 沒被解析，正是 #946 parser 要解的，但 pin 未升所以尚未生效）。
  - **agy `gemini-3.8-flash-high`：不可用**（#945，exit 0 但無 terminal envelope，三次耗盡 `card-terminal-schema-retry-exhausted`）。
  - **copilot `gpt-5.4`：唯一可用 builder**。reviewer／planner 仍是 claude sonnet。
- run-scoped builder override：`--payload <file>`，內容 `{"builder_executor":"copilot","builder_model":"gpt-5.4"}`（**只吃檔案路徑**，不吃 inline JSON）。

## 5. 本輪新學到的管線要訣（補 09-21 版 §5）

### 5.1 「候選歸檔 openspec」不是 builder 違規（#953）

`chore(openspec): archive <change>` 是 Manager 的 `openspec-archive` 卡（`work_bridge.py` 建 commit）。下裁決前先查 `jobs.json` 有無 `*-openspec-archive-*` job。真缺陷是 **builder 自產的測試／證據 doc 寫死 `openspec/changes/<change>/…`**：Manager 一歸檔就同時打斷 R-22（懸空引用）與那些測試（`FileNotFoundError`），而 verify 段（歸檔前）全綠、ship 段才炸。另：`work_actions._validate_local_archive_inputs` 讀 active 路徑，所以候選**自己**先歸檔會擋 `OpenSpec tasks unavailable`。裁決要寫「同時支援 active 與 `openspec/changes/archive/*-<change>/`」。

### 5.2 `copilot-review-timeout` 是本批的固定停點（#948）

Copilot 在 **PR 建立當下**就審完該 head，Manager 之後才 `request_copilot`，既有 review 早於 `requested_at` 被視為不存在 → 15 分鐘必逾時。出口：`review-attest`（maintainer 路徑，`_recoverable_maintainer_ship_stop` 認 `copilot-*`）→ `resume`。**若 operator 已場外把 main 併進 PR 分支，PR head ≠ candidate → in-lane ship 會 head-race**，那時只能場外 merge＋`retire-delivered`。所以場外 merge 只在「這條 run 確定不再回管線」時做；做早了要 `git push --force-with-lease=<branch>:<remote-sha> origin <candidate>:<branch>` 退回（lease 值用短 SHA 才過）。

### 5.3 三支 retry API 的實際判準（同一條 run 會三種都被拒）

- `retry-build`：看「最後一個 `status=exited` 且 `exit_code==0` 的 builder job 的 `workflow_evidence` 是否為 `None`」。provider 失敗（exit 1）時沒有這種 job → `requires unbound terminal builder evidence`；agy 那種「exit 0 但沒 envelope」反而**滿足**條件。**不可帶 `--expected-run-id`**（#936）。
- `retry-card`：只重派「最早一張尚未採信的卡」；該卡歷史上有採信過的 evidence 就 `refuses a card with accepted evidence`。**必須帶 `--expected-run-id`**。
- `resume --payload`：**不吃** `model_chain_override`（只有 `start`／`intake`／`retry-build`／`retry-card` 吃），也不清 `card-terminal-schema-retry-exhausted`。

### 5.4 兩個收割期的坑（都由 operator 手動修好）

- `ValueError: workflow input snapshot file missing`：Manager seed 的 `docs/superpowers/plans/<work_id>.md` 在 operator checkout 是 **untracked**，builder 誤提交後又刪掉 → terminalize 失敗。修法：依 job 的 `workflow_input_snapshot[].path`／`sha256` 從 operator checkout 複製回 job worktree（雜湊要相符）→ `resume`。派工 reason 要明寫「不要 commit／刪除該 plan 檔」。
- `WorkspaceError: job workspace commit bundle does not carry feature/<slug>`：copilot 照 repo `CLAUDE.md` 的 `wt/<feature>/<subtask>` 慣例另開分支提交，bundle（由 wrapper 以 `symbolic-ref HEAD` 產生）只帶 `wt/...` ref。修法：job worktree `git checkout -B feature/<slug> <clean-candidate>`，再依 `job_workspace.build_bundle_command` 的形狀重建：
  ```bash
  git -C <job-worktree> bundle create <spool>/<job>/commits.bundle.part \
      refs/heads/feature/<slug> ^refs/cortex/base \
    && chmod 0644 <spool>/<job>/commits.bundle.part \
    && mv -f <spool>/<job>/commits.bundle.part <spool>/<job>/commits.bundle
  ```
  然後 `resume`。派工 reason 要明寫「留在 Manager 給你的分支，不要開 `wt/` 分支」。

### 5.5 「manager daemon 未就緒（stalled）」多半不是壞掉

`control/client.read_status` 在 daemon pid 活著但 `status.json` 超過 120 秒沒更新時判 `stalled`；而一次 work-action 可能跑 7 分鐘（期間 status 不刷新）。處置：看 `~/.agents/control/cortex/status.json` 的 `updated_at` 年齡，≤90 秒再送；**不要重啟 daemon**。CLI 30 秒 timeout 之後用 `cortex request wait <id> --timeout 540` 追。

### 5.6 監看腳本要自帶退出條件

只在「狀態變化」時輸出的 watcher，遇到「所有 run 都 parked」會靜默數小時。本輪改成「全 parked 連續 2 分鐘 → 印一行 `ALL PARKED` 並 `exit 0`」，背景任務結束才會通知。腳本在 operator 的 session scratchpad，Codex 接手時自行重建即可（讀 `~/.agents/coordinator-cortex/jobs.json` 的 `workflows`／`jobs`）。

### 5.7 其他

- `authority-restart` 會丟掉已 exit 0 但未收割的 reviewer job 成果，且**不回收 reviewer sandbox** → 重派同候選同卡撞 `stale reviewer sandbox requires reconciliation`（#579 已補例）。處置：確認 pid 已退出後 `mv <review-sandboxes>/<hash> <hash>.stale-<job>`（不刪）→ `resume`。
- merge 前先確認 checks 已排入：`until gh pr checks N | grep -q .; do sleep 10; done`；`gh pr checks --watch` 在 checks 尚未出現時會立刻返回。
- `Architecture HTML` workflow（PR 動到 `README.md` 就觸發）的 `review` job 會 flaky fail，重跑即綠。

## 6. 本輪新開票／補證據

- **#953**（新）：builder 產出的 tests／docs 引用 active openspec 路徑，Manager 歸檔即全斷；附帶 verifier 把過時 operator ruling 當阻斷事實、且宣稱未實測的閘門結果。
- **#810** 補例：#819 走 maintainer 路徑 merge 後卡 `todo-incomplete`，`next_actions` 只給 `abandon`，operator 用 `retire-delivered` 收；建議把 todo 完成度檢查移到 merge 前的 pr-preflight。
- **#579** 補例：authority-restart 路徑未回收 reviewer sandbox。
- **#936** 實證：`retry-build --expected-run-id` 被 Manager 拒。

## 7. 下一批候選與順序

1. **先收尾在飛兩條**（§3）；#948 落地後 `copilot-review-timeout` 就不再是固定停點。
2. **升 pin**（§4 的前置檢查做完再升）；升完才有 #946 的 reset-hint parser 與退避，luna 額度事件才會被記住。
3. B2 續：#836 剩餘 scope（#866 是它的 A）→ #839；#835 三件套齊但 **Red 8，需先拆**。
4. B3：#497（三件套齊、`domain_breadth 1`／`state_consistency 2`，重 `registry.py`，**等 #862 merge 後**再派以免衝突）、#843。
5. B4：#895、#912、#841、#845。
6. 平台缺陷小票（都已有票，可直接進件派工）：#943（ship push 前 merge main）、#945（agy 背景任務）、#579、#936、#953、#874、#887。

## 8. 接手 checklist

- [ ] `hostname`、`systemctl --user show cortex-manager.service -p ExecStart`、`git log -1 origin/main` 三者核對；pin 是否仍為 `442fe23f`。
- [ ] `cortex status`：`in_flight`、`attention`（`hamanpaul/session-health` 的三件不歸本線）。
- [ ] 讀本文件 §1／§3／§5，再讀 #868 §0.1–§0.3。
- [ ] 每張票先 `gh issue view N --comments`＋`grep -n "#<N>'" .cortex/work-items.yaml`＋離線實算 sizing（`work_bridge.current_sizing_snapshot(workspace_root=".", combo_name=…, artifact_rows=[spec,design,plan])` 回 `(score, band)`）。
- [ ] policy_check 一律帶 PR 上下文（裸跑會假過）。
- [ ] `hippo`／`conventions` instance 不動。
