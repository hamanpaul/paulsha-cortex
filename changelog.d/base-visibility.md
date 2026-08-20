# base-visibility

- **`#731` (C)：run 的候選 git base 攤到 `cortex status`／`cortex work show` 上，並在過舊時給具名診斷。**
  0819 深夜現場逐字——候選 worktree `git rev-parse HEAD` ＝ `59a7a9b`（0818）、mirror
  `refs/remotes/origin/main` ＝ `7eb707b`（落後 13 支 PR），而 `cortex status`／`work show`
  的**任何欄位都看不到上面任何一個**。這個事實只存在於檔案系統上，operator 要看只能
  `sudo git -C <候選 worktree> rev-parse HEAD`。而 run 上唯一顯眼的「版本」欄位
  `source_revision: 22b88b01e9b2…` 是 **64-hex 的 authority digest**（work item 來源材料的
  sha256，見 `claim.semantic_source_revision`），與 git base 無關——那晚它把診斷帶偏了兩次
  （先誤判成 mirror 沒 fetch，補 fetch 後才發現 fetch 也沒用）。
  - **權威來源不新造第二份**：新模組 `coordinator/candidate_base.py` 只把既有欄位接到曝光面
    ——優先 `run.frozen_readiness["base_sha"]`（#211 pre-claim readiness 凍結集），沒有凍結集
    時退回**該 run 第一張 build 卡的 `job["dispatch_head"]`**（`manager._dispatch_workflow_card`
    對第二張起的 build 卡取的就是 `builder_jobs[0]["dispatch_head"]`，同一個欄位）。實機 0820
    逐字量測：**29 個 run 的 `frozen_readiness` 全為 `null`**，唯一記著候選基底的正是後者。
  - **距離唯讀**：`git rev-list --count <base>..refs/remotes/origin/main`，跑在 mirror
    （`PSC_REPO_ROOT`）上**現有的** remote-tracking ref。**絕不 fetch**（fetch 是 claim 的職責）；
    輸出以 `fetched: false` ＋ `measured_against: mirror:refs/remotes/origin/main` 誠實標示
    比較基準是「mirror 上次 fetch 的 main」。有測試釘住 status 路徑只發 `rev-parse`／`rev-list`。
  - **具名診斷**：落後達 `CANDIDATE_BASE_STALE_THRESHOLD_COMMITS`（預設 **10**，可用
    `PSC_CANDIDATE_BASE_STALE_THRESHOLD_COMMITS` 覆寫；門檻只有一處定義）時，`reason` 為
    機器可讀的 `candidate-git-base-stale`，而不是塞進自由文字的一句話。10 的來由：0819 一天
    之內 main 前進 13 支 PR，門檻取「約一個工作天的 main 移動量」的保守下界。
  - **fail-soft 但說得出口**：讀不到 mirror／算不出距離時，`behind_origin_main` 落
    `<unresolved:MirrorRootUnset>`／`<unresolved:MirrorMainUnreadable>`／`<unresolved:BaseNotInMirror>`
    （沿用 repo 既有的 `<unresolved:…>` 慣例，三種分開不塌縮），`reason` 為
    `candidate-git-base-distance-unresolved`；run 還沒有基底時為 `candidate-git-base-absent`。
    一律不靜默省略。
  - **命名與 `source_revision` 分離**：欄位叫 `candidate_git_base`，字面就寫著 git base，不與
    `source_revision` 共用任何詞彙；`work show` 印出 git base 時**一併點明** `source_revision`
    是 authority digest。`WorkflowRun.source_revision` 與 `claim.semantic_source_revision` 的
    docstring 也補上這條——那個誤導本身就是缺陷的一部分。
  - 曝光面：`cortex status` 的 `attention`（workflow run）與 `in_flight`（每張卡的
    `dispatch_head`）條目、`cortex work show`（文字模式與 `--json`）。Monitor 側走既有的
    `observations` 通道（`candidate_git_bases`），**不新增 WorkflowRun 欄位**——#261／#527 已
    付過那個學費（新增 row 欄位會讓整份 workflow projection degraded）。
  - **與 (A) 共用單一導出點**：(A)（`work_actions._refreeze_base_action`，寫入端）與 (C)
    （曝光面，讀取端）都要回答「這條 run 現在凍結在哪個 base」。那是同一個事實，因此
    `frozen_readiness["base_sha"]` 的正規化／驗證抬成 `candidate_base.frozen_base_sha()`，
    兩側**呼叫同一支函式**（有測試以 `is` 斷言同一物件），不再是兩份表述——本 repo 已經被
    這個形狀咬過（#727 的第二份 `-o` 落點、#728 的兩份 `next_actions` 導出）。
    凍結集為 `None` 時兩側的後續處置**刻意不同**且已寫進 docstring：(A) 退回來源樹本地
    `refs/heads/main`（下一張卡**會**用什麼基底），(C) 退回第一張 build 卡的 `dispatch_head`
    （候選**已經**坐在哪個 commit 上）——同一條時間軸的前後兩點，不是矛盾。(A) 重新凍結成功
    之後凍結集就存在了，(C) 自動改讀凍結集、`sha_source` 變回 `frozen-readiness-base-sha`、
    `behind_origin_main` 歸零，兩半因此機械地接得起來（有測試釘住，並涵蓋 (A) 寫入的
    `cortex-candidate-base-freeze/v1` schema）。
  - 範圍：本 PR **只做 (C)**（可見度）。(A)（重新凍結的入口）已於 #733 落地；hermetic pinning
    本身一字未動。
