---
type: feat
scope: trust-root
---
**R0.5 D6 / trust-root Phase 2a：review verdict 受控通道（per-job 單向 spool）——堵掉
spec 揭露的最短攻擊路徑「builder 直接代寫 reviewer 的 verdict 檔」**

spec `trust-root-isolation-spec.md` 背景 §3 認定的最短攻擊路徑是：verdict 由 reviewer
模型寫在**自己的 worktree 內**（`.psc-review-verdict.json`），而在同 UID 下 builder job
可以直接寫進去——不需要偷任何 capability、不需要繞過任何 gate，就能替自己的 candidate
產出一份「通過」的 foreign review；原有的 pre-seed 守衛只在**啟動前**檢查一次，攔不到
之後的覆寫。§R2 因此要求 verdict「MUST NOT 停留在同 UID 可寫的 worktree 內作為權威
來源」。未決 10-6 列了兩案（Manager socket 回傳／per-job 單向 spool），本次採
**per-job spool**（較簡單、可先落地），**待 operator 最終拍板**。

- **落點搬離 worktree**：新增 `config/paths.py:review_verdict_spool_root()`
  （`<coordinator_root>/review-verdicts/`）與 `coordinator/review.py` 的唯一 per-job
  定址 `review_verdict_spool_dir()`／`review_verdict_spool_path()`
  （`<root>/<reviewer_job_id>/verdict.json`）。登記進 R1 資產登記表為
  `review-verdict-spool`（Tier-0，ingress `INTERPROCESS`，tree 分類比照既有的
  `monitor-event-spool`）——permgen 產出的實質是 **Manager-owned**：容器 owner＝
  `durable_state_owner`、mode `0700`、reviewer 僅 **write-only** ACL（`wx`，寫得進
  自己那格、讀不到他人 verdict）、**builder 零寫入**（兩個 UID 方案皆然）。
- **防預埋／防覆寫**：`prepare_review_verdict_spool()` 在 dispatch 前建立該 job 的
  spool 目錄（`0700`）並執行 pre-seed 守衛——目錄或 verdict 檔已存在／是 symlink
  一律拒絕派工（舊 worktree 守衛保留為 defense-in-depth）。Manager 落地後
  `seal_review_verdict_spool()` 把 spool verdict 轉 `0444`。
- **身分由 Manager registry 推導，payload 自述被忽略**：新增
  `read_spool_review_verdict()`——`builder_job_id`／`reviewer_job_id`／`candidate`／
  `launch_identity` 四個綁定欄位**不看 payload**，一律由 job registry
  （reviewer job row 的 `executor`／`model_id`／`independence_domain`）推導後覆寫；
  payload 裡出現同名鍵直接丟棄並回報。reviewer 真正貢獻的只有 `findings`。這與
  workflow lane 早已成立的形狀（`terminalize_workflow_job` 自組 verdict payload）
  對齊，不另立一套。reviewer prompt 的 verdict template 同步縮到只剩
  `schema_version`／`findings`。
- **相容遷移**：reviewer job row 新增 `review_verdict_channel`（唯一合法值 `"spool"`，
  其他字面值 fail-closed）。**有標記的 job 只認 spool 落點**——找不到就是
  `verdict-missing`，不回退；沒有標記的（本次修法之前派工的 in-flight job）才讀
  legacy worktree 落點，並記 WARN ＋ `DiagnosticReason`
  （`review-verdict-legacy-worktree-source`）＋ slice action
  `foreign-review-legacy-verdict-source`。標記放在 jobs.json 而非靠「spool 目錄在不在」，
  否則 builder 只要刪掉 spool 再寫 worktree 就能把自己洗回 legacy 路徑。
- **launcher 窄放行**：verdict 搬出 worktree 後，executor 自身 sandbox 會擋掉寫入
  （codex `--sandbox workspace-write` 只放行 cwd）。新增
  `SubprocessLauncher.as_verdict_spool_writer()`＋三個 argv builder 的
  `verdict_spool_dir` 參數，沿用既有 `--add-dir` 只放行**該 job 的那一格**
  （不是整棵 coordinator 樹）；read-only／review-only 契約顯性拒絕；未授權時 argv
  與改動前逐字相同。Manager 端以 `getattr` 取用，注入的 fake launcher 不受影響。

**範圍與誠實邊界**：本 PR 只改 **slice lane**（`_launch_foreign_review` 與
`_finalize_review_job`）。**workflow lane 無 verdict 檔可搬**——它從 reviewer job 的
終局 JSON（`_extract_terminal_json(job["log_path"])`）收割，本來就不在 worktree 內，
且綁定欄位早已由 Manager 自組；故該 lane 未動。**Phase 2b（分 UID／chown）之前，同
UID 下 builder 技術上仍寫得進 `coordinator_root`**（Phase 1 自檢已知並 WARN）；本階段
交付的是**通道結構**——路徑、守衛、R1 登記、身分推導與 permgen 等式，OS 強制等 Phase 2b。

新增 `tests/test_review_verdict_channel_p2a.py`（43 測試）。
