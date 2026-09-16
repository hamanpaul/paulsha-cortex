# Open-issue 全量盤點與 refine 開工 handoff

日期：2026-09-16（Asia/Taipei）
機器：9900X（WSL2）
接手目標：依 #868 開始 refine 實作。本文件是「前一 session 做了什麼、現在狀態是什麼、開工先做什麼、最近用 cortex 撞到什麼」的交接，不是新的設計文件。

## 1. 停止邊界

- 本輪只做 **issue 盤點／關票／一條既有候選的 PR**，沒有動 refine 的任何產品實作，沒有動 daemon、runtime pin、overlay、service。
- `main` 現在是 `ad172733`（PR #913 merge commit）。
- 本 handoff 由 `feature/868-refine-handoff-20260916` 分支交付；除此分支外主 checkout 無未提交變更。

## 2. 本輪落地

| 項目 | 結果 |
|---|---|
| 145 張 open issue 逐票對照 main commit／merged PR／留言／原始碼 | 分三類：38 已修未關、8 過時／降級、99 真待解 |
| 38 張已修 → 留證據留言 `close --reason completed` | #476 #478 #482 #484 #485 #487 #490 #499 #500 #501 #506 #507 #508 #509 #511 #514 #515 #516 #519 #520 #524 #527 #534 #535 #536 #540 #545 #548 #549 #554 #568 #569 #588 #601 #604 #635 #723 #827 |
| 8 張過時 → `close --reason "not planned"` | #563 #580 #589 #591 #595 #706 #837 #853（依 #868 §4 的降級判決；#853 被 #879 修法取代） |
| #800／#815 候選 `feature/800-installer-shared-config-guard`（cortex run 產物、從未推 origin） | merge main → 全套 5891 passed → policy_check 0 fail → PR #913 → Copilot review 3 條（1 駁回、2 採納）→ merged `ad172733`，#800／#815 自動關閉 |
| #868 正文 | 追加 §8「2026-09-16 盤點增補」，記錄表內 137 張的現況與 15 張表外新票 |

Open issue：145 → **97**。

## 3. 現況快照（2026-09-16 06:40Z）

### 3.1 Issue 面

- 97 張 open：82 張在 #868 §5 表內、15 張是 09-11 之後的新票（#871 #874 #875 #876 #877 #878 #881 #882 #883 #885 #887 #895 #897 #911 #912），全部沒有修復 PR。
- #868 表內仍 open 的 Level 分布：P1 56、P1-條件 1（#838）、P2 21、V 2（#486 只剩 slice-lane prompt 缺 enum；#716 明定等 production canary）、M 2（#829／#868）。
- #829（十四類交付 master）直接點名且仍 open：#496 #497 #819 #821 #825 #826；其 child（#835–#850、#862、#866）宣告的模組 `execution_profile.py`／`executor_backoff.py`／`quota_observation.py` **都還不存在**，PR #832–#867 只是 docs(refine) 進件。

### 3.2 本機 cortex runtime

| 欄位 | 現值 |
|---|---|
| daemon | `cortex-manager.service` active，PID 43892，`--no-require-idle`，interval 600s，`PSC_COORDINATOR_ROOT=$HOME/.agents/coordinator-cortex` |
| daemon 載入的 pin | `$HOME/.agents/runtime-pins/cortex-75565400-20260915b`（= PR #896 merge，#879 monitor 修法） |
| pin 落後 main | PR #905 #906 #908 #909 #910 #913 未載入——特別是 #913（installer）與 #910（agy `--print-timeout`）|
| pipx `cortex` CLI | 0.1.8，讀不懂 pin 寫的 diagnostic schema v2；看真狀態要用 pin 的 code（見 §5） |
| monitor | `cortex-monitor.service` active（#879 修法後不再堆 thread） |
| 其他 instance | `conventions-*`、`hippo-*` active；`idk`／兩個 failed unit 未處理 |
| status | daemon idle、`tick_circuit_open=false`、`degraded=false` |
| attention（1） | `fix-rate-limit-classification` run `workflow-87ef197f9f79c2acae1b`：`brainstorm-not-ready`，planner claude-opus-5 回 `stop_reason: refusal`，被歸 `content`，`next_actions=[abandon]`。對應 issue #506 **今天已關**，這條 run 應 `abandon` 後不再 intake |
| not_claimable（3，皆 `missing_issue`） | `bucket-c-evidence-dedup`（workstream，維持）；`installer-shared-config-guard`（PR #913 把 `openspec/changes/installer-shared-config-guard/` 以 active change 帶進 main、未 archive，06:23Z 起每輪掃到）；`trust-root-agy-builder-grant`（09-06 起） |
| in_flight（1） | `wf-f76fa26d28-verification-565` claude/sonnet，paulshaclaw#369（cockpit） |

## 4. refine 開工建議（從 #868 §3 拉最小集合）

#868 的原則：**不把 137 張平行開工，沿一條例外 journey 拉最小缺口**。本輪已驗證「仍在」且有精確位置的缺陷，可直接當第一批：

| 批次（#868 §3） | 建議先拉的票 | 本輪驗證到的精確位置 |
|---|---|---|
| B0 執行基礎 | #818 多 Manager 覆寫 jobs.json；#819 not-idle 熱迴圈；#821 no-op 整檔重寫；#496 dirty recheck 每 tick 追加 | `manager.py:2270-2305` dirty recheck 無條件 `_apply_verification_result`（09-07 實測 jobs.json 58.7 MB、單 slice 92k 筆 evidence_history） |
| B1 假阻擋＋裁決直達 | #830 非 Job decision KeyError；#814 `retry-build --reason` 不進 builder prompt；#503 pinned spec body 未送 builder | #814 留言實證：裁決要繞 reviewer 一輪（約 40 分鐘）才到 builder |
| B2 例外換路／等待 | #826 taxonomy 補三類；#825 quota 持久退避；#807 agy fallback terminal；#600 roster vs CLI 可用性 | #807：PR #820 已在 pin `79ba6447` 仍於 09-11 再命中，status enum 別名不在 #820 範圍；#874 是同族的 verify/review explicit-stop 缺口 |
| B3 拆分與恢復接續 | #497／#481 terminal 重播；#479 slice-action retry-build；#577／#578 retry 只重置；#613 abandon 不回收 branch；#555 無熔斷；#557 regenerate-gates 無 `--card` | #479：`manager.py:2026` `apply_slice_action` retry-build 呼叫 `dispatch_ready_fn` 仍未傳 `launcher_factory`；#613：`work_actions.py:4308-4317` refreeze-base 明確 raise 指向本票；#555 只有 `redispatch_count` 鉤子沒有上限 |
| B4 結案與觀察 | #895 已交付無 CompletionRecord 永遠 todo；#808／#810 closure 死結；#912 status 缺終態；#911 ship lane 硬要 openspec | #895：PR #905 的 `work link` 只是治標，46 件 `missing_issue` 會再長 |

開工前的三個一次性動作：

1. 把 daemon pin 推到含 #913 的 main（新 pin → 改 `cortex-manager.service` 的 `PYTHONPATH` → `systemctl --user daemon-reload && restart`），否則 refine 期間 installer／agy timeout 的修法都不在載入版本裡，會重複撞已修的問題。
2. 處理 §3.2 的 attention 與 not_claimable：`abandon` `fix-rate-limit-classification`（#506 已關）；`installer-shared-config-guard` 走 `openspec archive`（或先 `cortex work link ... --issue 800` 治標）；`trust-root-agy-builder-grant` 補 issue 或 link。
3. 若要讓 cortex 自己跑 refine 的 child（#849／#850／#866 等），先確認 roster 只放實測可跑的身分（見 §5 第 4 條）。

## 5. 最近用 cortex 遇到的問題（operator 視角，2026-09-06 → 09-16）

這些是實際撞到、且大多**還沒有對應修復**的問題；refine 派工時會再遇到。

1. **CLI 與 daemon 不同源**：pipx `cortex` 0.1.8 讀 pin（0.1.10，diagnostic schema v2 `next_step_hint`）寫的 `jobs.json` 直接 fail-closed。要看真狀態：
   ```bash
   cd ~/.agents/runtime-pins/cortex-75565400-20260915b && . ~/.agents/core/runtime/cortex-manager.env && python3 -m paulsha_cortex.cli status
   ```
   對應 #841（loaded artifact 與 config revision 對照）、#877（ensure-running 單一入口）。
2. **daemon 讀本地 checkout，不讀 origin**：`project-cortex.yaml` 指向 `~/prj_pri/paulsha-cortex`／`paulshaclaw`／hippo 的工作樹；本地落後 origin/main 時 `work link`／PR 都到不了 daemon。planner 在 operator worktree 跑 brainstorm 期間對它做任何事（連 `git pull` 改 mode 0644→0664）都會判 `planning launcher modified operator worktree` → `needs_human`。對應 #507 家族的殘項 #551。
3. **planner 模型 refusal 被歸 content**：`fix-rate-limit-classification` 的 brainstorm 由 claude-opus-5 回 `stop_reason: refusal`，taxonomy 判 `content` → 只剩 `abandon`，沒有換 planner 的路。對應 #826（taxonomy 補訊號）、#572（環境類拒收歸 content）。
4. **roster 身分與 CLI 實際可用性脫鉤**：帳號 model 快取沒有 `gpt-5.3-codex-spark`，codex CLI 回 400「not supported when using Codex with a ChatGPT account」（不是額度）；copilot `mai-code-1.1-flash` 拒 launcher 預設 `--reasoning-effort xhigh`，要用 `gpt-5.4`。dispatch 燒掉 job 才發現。對應 #600、#826、#483。
5. **agy 當 verifier／reviewer fallback 時 terminal 不相容**：09-08 claude 429 期間 agy/gemini-3.1-pro-high 的 verifier exit 0 卻 `workflow verification terminal schema invalid`／`terminal log has no JSON evidence`，都在含 PR #820 的 runtime 上。對應 #807；同日 claude opus 的 verifier 誠實回 `status: failed` 也掉進 `ValueError`（#874）。
6. **Claude builder commit 被 acceptEdits 擋**：09-11 paulshaclaw#341 `tdd-red` 卡，builder 寫完全部 RED 交付物後 `git add` 要 approval，回 `needs_human` HEAD 不動（$9.47／24 分鐘）。PR #859 只是部分修。對應 #480。
7. **同 issue 綁兩個 work item = `CorrelationError`**：整份 override 失效、provider degraded；v1/v2 重識別只能綁一邊。
8. **docs-only 工作要 operator 先寫三件套**：沒吃到 issue 任務清單的 planner 會把 scope 縮成空心交付（PR #907 只加一支 test）。要 define 確定性跳過，spec/design/plan 必須 accepted、含 Requirements／Decisions／Tasks 標題、Open Questions 為空。`todo.md` 是 pinned authority，drift gate 只容忍 checkbox 翻轉。
9. **ship lane 硬要 1 PR＋1 openspec＋1 todo**：small-fix combo 無 openspec 卡、feature-oneshot 的 openspec-propose 可能確定性 passed 但無產物 → `multiple-delivery-targets-unsupported`；`review-attest` 也要 openspec＋PR。這輪 #824／#904 都是 operator 場外 merge 再 `retire-delivered`。對應 #911、#885。
10. **46 件 `not_claimable: missing_issue`**：superpowers plan/spec/todo 一律 active、strict closure 是唯一 done 出口；用 `cortex work link` 綁 CLOSED issue 只是治標（PR #905／hippo#154），今天 #913 一 merge 又長出一件（§3.2）。對應 #895 本體。
11. **recovery CLI 參數坑**：`retry-build`／`retry-review` 的 `expected_candidate` 只能走 `--payload <json>`；`retry-build --reason` ≤4000 字會進 repair 回合但 builder 讀不到（#814）；`retry-review` 不收 reason；`recover-planning` 要 `--failure-classification` 且 `--failure-reason` 必須逐字等於 evidence 的 reason；`retire-delivered` 接受 closed_unmerged PR 但 run 自己要有 pr_refs；`abandon` 只准 pre-delivery 且無 active job。對應 #843（正式入口一致性）、#864。
12. **adversarial reviewer 在 sandbox 無 `agy` 時把真 transcript 判為不可重現**：#824 那輪要 operator 本機重跑五列逐字相符後場外裁決。對應 #803（verifier 環境不相容）。
13. **monitor thread 風暴**：09-11 `cortex-monitor` 88,251 threads／5.3 GB，provider stale → 新 work item 被 `provider_degraded_freeze` 凍在 `topic`。#879 已修（pin 75565400 已含），#827 同步關閉；`hippo`／`conventions` instance 若還在舊 pin 要另外升。
14. **jobs.json 膨脹**：default root 曾 58.7 MB、每 2–11 秒整檔重寫；換 `coordinator-cortex` root 後目前 3.1 MB／551 jobs，但 #496／#821 的機制沒修，會再長。
15. **跨 checkout 的平行 agent**：Codex 與 Claude 常同時在同一個 checkout／cortex instance 上動。本輪派 subagent 一律用獨立 git worktree；agent 結束後 worktree 會以 locked 狀態留下（`.claude/worktrees/agent-*`＋`worktree-agent-*` 分支），要手動 `git worktree unlock/remove` 與 `branch -D`。
16. **`gh` 小坑**：`gh issue view --json` 沒有 `stateReason` 欄位，要 `gh api repos/.../issues/N --jq .state_reason`；連續 comment/close 50 張沒撞 secondary rate limit（每張 `sleep 3`）；在 UTF-8 locale 下 `grep -E "[^0-9]#N"` 對含中文的行會漏，要 `LC_ALL=C`。

## 6. 新 session 開工 checklist

- [ ] `hostname`＋`systemctl --user show cortex-manager.service -p ExecStart`＋`git log -1 origin/main` 確認機器、pin、main 三者。
- [ ] 讀 #868 §0.1–§0.3（正事優先、受理後離場、STOP 線）與 §3 批次表；讀本文件 §4 決定第一條 journey。
- [ ] 做 §4 的三個一次性動作（升 pin、清 attention／not_claimable、核 roster）。
- [ ] 每張要動的票先 `gh issue view N --comments` 看最新留言——本輪只複核到 09-16，且有些票（#807／#480／#874）的最新現場證據在留言不在正文。
- [ ] 修完的票要在 cortex 走到 strict closure（PR merge＋issue closed＋openspec archive），否則 #895 的 `missing_issue` 會再長；operator 場外 merge 記得 `retire-delivered`。
- [ ] 不要重做 §2 已關的 46 張；若再命中，附 pin／run／job id 重開。
