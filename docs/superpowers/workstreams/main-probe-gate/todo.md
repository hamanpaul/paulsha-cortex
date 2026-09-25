---
status: accepted
work_item: main-probe-gate
invariant_count: 7
artifact_classes:
  - source
  - tests
  - documentation
domain_breadth: 0
state_consistency: 2
acceptance_surfaces: 2
spec_stability: 0
orchestration: 2
total_score: 6
sizing: yellow
---

# Main probe gate todo

## Tasks

- [x] Source: 在 `work_bridge.py` 加 typed success/failure（含 optional `main_head`）、full commit SHA/object validation，以及 direct bounded subprocess probe；逐一保留 Candidate resolve/validation、fetch、FETCH_HEAD resolve/validation、merge-base、merge-tree、path parser 的 stage、returncode 或 timeout-null、error_kind，不使用 `base_sha_probe` 或無 timeout contract 的 default runner。failure 必須經現有 content-addressed delivery evidence writer 保存結構化 Candidate／stage／returncode／error_kind／main_head，並讓 Manager stop 結果帶回 evidence ref/hash。
- [x] Tests: 用 `merge-tree -z` 做 NUL-safe conflict path parse；涵蓋含空白及換行的多條 path、CHANGELOG/top insert classification、README/multiple conflict、clean-behind、M-preserving post-fetch failures、bad SHA、非 commit object、unsupported option、command timeout、preflight 間 main 前進、merged PR、terminal refresh。
- [x] Tests: 經 production Manager needs_human wrapper 寫入 probe failure 的 durable delivery evidence；由 stop 結果取得 ref、重新讀取 content-addressed JSON envelope、驗 hash 並逐欄比對 Candidate／stage／returncode／error_kind／main_head。第一次 failed fetch 必須讀回實際 `fetch` nonzero returncode 且 `main_head=null`；已取得 M 後的 merge-base／merge-tree／path-parser failure 必須讀回同一精確 M 與原 stage/returncode/error_kind，timeout returncode 為 null。不得用 in-memory object 或 detail prose 作證據。
- [x] Tests: 以專用 bare-origin 驗證 in-sync Candidate 的兩次 probe 都看見 C 含 M，允許 preflight 並到達只寫本機 fixture 的 push；behind/conflict/failure 必須不 preflight、不 push、不建 PR、不 request Copilot。
- [x] Tests: bare-origin fetch failure → production Manager `main-sync-unavailable` needs_human stop → 先由 stop 的 evidence ref 讀回並斷言首次 `stage=fetch`、實際 returncode、error_kind 與 `main_head=null` → 修復 fixture remote/ref → 使用 production operator workflow-resume path → ship validator 再 probe，斷言第二次 fetch 與修復後精確 M。此 delivery evidence read-back 不取代 child 04 的 `needs_human_reason.context.main_sync` durable read-back；不能把非-define `_claim_action` 的 `workflow_starter` no-op 算成 resume 成功。
- [x] Documentation: 更新 lifecycle docs、changelog fragment 與 `[Unreleased]`；加入 CLI help parity smoke 證明 command help contract 無變；跑 focused 與 full repository gates、network guard。CHANGELOG 只分類唯一 `[Unreleased]` 雙邊頂端插入，本票不修檔、不派 Builder、不產生 merge commit。

## Sizing inputs

`invariant_count: 7` 對應 live #987 的七項驗收條件；`artifact_classes` 為 source、tests、documentation。Production scope 僅一個 module，故 domain breadth 0；兩次 probe 間 `origin/main` 可前進，故 state consistency 2。`fix-standard` 有 2 個 gate_spine 與 R-09/R-16/R-19，acceptance surfaces 為 2；三件 accepted artifacts 無 blocker，spec stability 為 0；9 cards、9 persona bindings，orchestration 為 2。正式 helper 結果應為 `6 / Yellow`。Durable delivery evidence 是現有 `work_bridge.py` evidence writer 的結構化失敗輸出及 read-back，沒有增加 production module 或 issue invariants。

## Dependencies and boundary

此票是 #972 第一個 descendant，無其他 descendant 前置；完成後解鎖 typed diagnostics、recovery-actions，最後再解鎖 Manager durable writer。對應 #972 的 probe timing/classification、typed failure、fail-closed、merged/terminal skip；#943 真 merge、Builder re-dispatch、D reverify/push 保留給 #973。
