# Refine B2 進度 handoff（#911 → #825 家族 → #866）

日期：2026-09-21（Asia/Taipei）
機器：9900X（WSL2）
前一份：`docs/handoffs/2026-09-17-refine-b1-b2-handoff.md`
接手目標：依 #868 §3 繼續 B2；本文件記錄 09-17 → 09-21 做了什麼、使用者裁決、runtime 現況、還沒做的工作、以及六條 cortex run 撞到的缺陷。

## 1. 使用者裁決（本輪新增，優先於本文件其他建議）

- **#825 拆兩個子 issue 派下去**（09-17）：拆成 #928（母 design C＋D＋parser）與 #929（E＋F），與 #850（A）並列；三者落地後 #825 關閉（09-21）。
- **#866 與 #928 平行派**（09-21）：允許同一 daemon 並行跑無檔案重疊的 run；代價見 §5.7（CHANGELOG 固定衝突、copilot quota 互搶）。
- 沿用 09-17 裁決：只 focus `cortex` instance；operator 只做進件、intake、needs_human 裁決、ship 卡住時場外交付；不在 job 內修平台。

## 2. 本輪落地（2026-09-17 → 09-21）

| 票 | 交付 | 方式 | 介入 |
|---|---|---|---|
| #911 ship lane 無 openspec | PR #927（main `668cd4f2`） | cortex `workflow-3e69cdfea110fdb99655` | 場外 merge＋`retire-delivered`（跑在舊 pin，ship 自己必停） |
| #850 store 本體（#825 A） | PR #931（`442fe23f`） | cortex `workflow-d6d1242b8aa02af4214b` | `retry-build` ×3（空心交付→完整；改 pinned spec/design→還原；改 todo 文字→還原）、場外解 CHANGELOG、py3.13 symlink 迴圈修補、採 2 Copilot nit |
| #928 terminal／parser／workflow admission | PR #940（`1ec2d732`） | cortex `workflow-082a71ce5f388bc2c198` | `retry-build` ×3（測試替身簽名、非 hermetic 測試、補 T5／T7 測試）、`retry-card` ×2、copilot rate limit ×2（CLI 掛住要 kill）、場外解 CHANGELOG、採 1 Copilot nit |
| #929 slice consumers | PR #941（`7b29418c`） | cortex `workflow-376fa6cb5081e585be6f` | preflight 在 daemon 環境失敗 2 次、本機同 env 皆過 → 場外 merge；採 1 Copilot finding |
| #866 quota observation core | PR #942（`a8323abd`） | cortex `workflow-7c2684c724334a01454c` | envelope 128 KiB 超限（#938）→ abandon＋excludes 重 intake；#613 舊分支改名；`retry-build` ×2（空心→完整；R-21 絕對路徑）；`retry-card` ×1（report 路徑筆誤）；`review-attest` 被 #847 擋→`resume`；場外解 CHANGELOG、採 2 Copilot nit |
| 進件 docs PR | #926（#911）、#930（#928／#929）、#932（todo 關鍵字）、#939（#866 excludes） | operator | #939 在 checks 尚未排入時就 merge（跳過 PR CI，事後 main Tests 綠）——見 §5.8 |

**#928 是第一條 ship 段全在管線內建 PR 的 run**（#911 生效＋preflight＋PR 建立）；#866 與 #929 亦由 Manager 自建 PR。merge 仍全靠 operator，卡點都是並行衝突或 CI-parity。

新開／補證據的票：**#938**（envelope 128 KiB 字面上限、同內容重複計入）、**#943**（並行 run 的 CHANGELOG 固定衝突，ship 應 push 前 merge main）、#847 補兩例（`plans/<work_id>.md` 觸發 restart）、#825 closed。09-20 你另開的 #933–#937 已分析（§6）。

## 3. 現況快照（2026-09-21 20:30 CST）

- `main` = `a8323abd`（PR #942）。Open issue 96。
- `cortex-manager`／`cortex-monitor`：pin `~/.agents/runtime-pins/cortex-a8323abd-20260921a`（= main），drop-in `.bak-442fe23f-20260921` 備份；pipx `cortex` 同日從本地 main 重裝。
- daemon idle、in_flight 0；attention 3 件全是 `hamanpaul/session-health` 的 `session-health-jev-*`（你 09-19／20 派的，PR #8／#9／#10，各停在 review／ship 段 needs_human）——**不在 cortex refine 範圍，本輪未動**。
- roster 未改：planning／review = claude sonnet、build = copilot gpt-5.4 → agy。**agy gemini-3.8-flash 當 builder fallback 實測不可用**（#928：40 秒、跑進別的 job worktree、無 envelope ×2）。
- `stale/866-run1-f13a568f` 本地分支是 #866 第一條 run 的候選（#613 workaround 改名保留），可刪。
- `openspec/changes/quota-observation-schema-core/` 留在 repo 未歸檔（#938 workaround 把它從 work item excludes）。

## 4. 未完成／下一步（照 #868 §3，不跳批）

1. **B2 續**：#836（#866 是它的 A；剩餘 scope 待拆或整票實算 sizing）→ #839；#838 在共池並行前。
2. **可直接派的候選**：#849 `execution-profile-schema-core`（三件套齊、Yellow 5）；#835 `execution-profile-contract` 三件套齊但 **Red 8**（需拆）。#600 無 work item。
3. **B3**：#497、#862、#843、#547／#555／#577／#578。
4. **B4**：#895、#912、#808／#810、#841、#845。
5. 平台缺陷建議優先修（每條 run 都撞）：#847（restart 清 finding）、#943（CHANGELOG）、#938（envelope）、#546（next_actions 只給 abandon）、copilot CLI rate-limit 掛住無 timeout（未開票，見 §5.3）、`policy_check.preflight` 丟棄 pytest 輸出（跨 repo，未開票，見 §5.6）。

## 5. cortex 派工實測要訣（本輪新增，補 09-17 版 §5）

1. **進件前先查既有 work item 與 sizing**：`grep -n "#<N>'" .cortex/work-items.yaml`；同 issue 綁兩個 work item = `CorrelationError`。sizing 離線實算：`work_bridge.current_sizing_snapshot(workspace_root=".", combo_name=..., artifact_rows=[spec,design,plan])`；Red 不要 intake（停 `needs_decomposition` 沒人接）。todo-only（無 spec/design）會因 `spec_stability=2` 算 Red，但 planner 補完後重算——要保證 Yellow 就自己寫三件套。
2. **combo 選擇**：issue 標題 scope 必須在 `deck/data/task-types.yaml` 的 7 個受控 scope 內，否則 `ComboSelectionError` → `--combo` 明示。`fix(coordinator):`／`feat(coordinator):` 會自動選。
3. **plan review 機械檢查**只掃 Tasks 下 list item **首行**找 `changelog`／`cli`／`test`／`doc` 關鍵字（`planning._collect_task_items` 丟續行）；缺 → `plan-review-retry-contract_compatibility`。
4. **裁決指令要明寫 pinned 邊界**：spec／design／todo 文字是 pinned authority，只准翻 checkbox；澄清寫進 terminal reason。否則長回合自審會「修文件對齊實作」→ `workflow planning input drift`。drift 判定：`manager._workflow_input_snapshot` 先比 operator checkout 再比候選樹，plan 類只容忍 `[ ]`→`[x]`。
5. **copilot builder 撞 rate limit 會掛住不退出**（`session.error errorType=rate_limit` 後無 result、進程活著）：`kill -TERM` wrapper＋node 子進程，Manager 會判 `rate_limited` 並 reroute（agy 不可用→再回 copilot→`provider-retry-exhausted`）；quota 回來後 `retry-card --card subagent-build --reason` 指示 `git merge --ff-only origin/<wip 分支>` 採用已完成的 commit（per-job clone 的 commit 不在主 repo：`git clone <job worktree>` 取出後 push 到 GitHub wip 分支，builder clone 會鏡到 `origin/*`）。
6. **preflight（CI-parity）的診斷死角**：`policy_check.preflight` 只留 `tests: FAIL (exit=1)`，pytest 輸出全丟；env 為 daemon PATH＋disposable HOME＋`LC_ALL=C`＋`VIRTUAL_ENV`（daemon 繼承了 paulshaclaw 的 venv）。本機重現：`env -i PATH=<daemon PATH> HOME=$(mktemp -d) LC_ALL=C python3 -m pytest tests/ -q`；或在 `/tmp/cortex-preflight-*/` 建分支 worktree 跑 `pipx-venv/python -m paulsha_cortex.preflight_ci --metadata <evidence/pr-metadata/<run>.json>`。#928 抓到真缺陷（測試 spawn 真 `claude`，要 seed `manager._EXECUTOR_AUTH_CACHE`）；#929 重現不了（疑負載 flake）。
7. **並行 run 固定衝突**：`CHANGELOG.md [Unreleased]`（每次）、`docs/unified-work-lifecycle.md`、`README.md`（同區塊時）。場外：`git worktree add --detach <scratch> <candidate>` → `git merge --no-ff --no-commit origin/main` → 衝突區塊兩邊保留（候選在上）→ commit → `git push origin HEAD:<PR 分支>` → 等 checks → merge → `retire-delivered`（run 有 pr_refs）。
8. **merge 前確認 checks 已排入**：`gh pr checks --watch` 在 checks 尚未出現時立即返回；先 `until gh pr checks N | grep -q .; do sleep 10; done`。
9. **review-attest 是 blocking-findings 的正規出口**（#911 C：無 openspec／尚無 PR 可用、可附 `evidence_refs`），但被 #847 的 digest 前進擋住時先 `resume` 讓 Manager 重錨（會重跑 verify／review／adversarial）。`retry-card` 對已採信的卡會拒（`refuses a card with accepted evidence`）。
10. **monitor snapshot 的 revision** 是 `local-sha256:<_digest>`，`_digest` 先餵 8-byte 長度再 sha256，**不等於** `sha256sum`；比對用同一算法。
11. **abandon 後重 intake 會撞 #613**（builder 分支未回收）：`git branch -m feature/<work_id> stale/...` 改名保留後 `resume`。
12. **daemon 重啟時機**：有 job 在飛時不要升 pin（#582）；等 `in_flight 0`。

## 6. 09-20 新 issue 分析結論（#933–#937）

- #933（agy envelope ERROR 與 verified 並存）：真新、未修；與 #807／#874 同族不同缺陷。
- #934（主 agent 工作範圍）：設計題；prompt 層已由 `~/.agents/AGENTS.md`（09-20 改版）部分落地，cortex 側 intake 契約與 #864 重疊，建議合併討論。
- #935（threads resolved 後仍 needs-fix）：**出口已存在**——`_ship_action` 在判 `needs-fix` 之前先看 `maintainer_review_path`，`review-attest` → `resume` 走 `_ship_with_maintainer_review`；缺的是 attention 沒列它（#546 家族），建議併入 #546。
- #936（retry-build 收 `--expected-run-id` 後被拒）：真新、CLI 側送件前擋掉即可。
- #937 六項：1 與 #897／#847 高度重疊（drift 第一段比 operator checkout）；2 屬 #578／#883 大類；3 **重複 #613**；4 為 #519 follow-up；5、6.1（scope 詞典寫死，本輪 #911／#850 也撞）真新小票；6.4 併 #812。

## 7. 新 session 開工 checklist

- [ ] `hostname`、`systemctl --user show cortex-manager.service -p ExecStart`、`git log -1 origin/main` 三者核對；pin 落後 main 就照 `cortex-runtime-pin-upgrade.md` 升。
- [ ] `cortex status` 確認 in_flight 為空；attention 的 session-health 三件不歸本線。
- [ ] 讀 #868 §0.1–§0.3、本文件 §1、§4、§5；下一張從 #836 剩餘 scope 或 #849 開始。
- [ ] 每張票先 `gh issue view N --comments`＋查既有 work item＋實算 sizing；修完走到 issue closed，run 用 `retire-delivered`／`abandon` 收乾淨。
- [ ] hippo／conventions instance 不動。
