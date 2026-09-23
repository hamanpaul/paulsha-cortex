---
status: accepted
work_item: main-probe-gate
---

# Main probe gate 規格

## Requirements

本 work item 的 production scope 只改 `work_bridge.py` 的 main probe 與 ship validator gate；tests 可經既有 Manager resume／validator seam 做整合驗收。

- 每個非 terminal ship tick 在第一次 preflight 前 probe `origin/main`；要 push 時 preflight 後、push 前再 probe。merged PR 走既有 closure；terminal refresh 不 probe。
- 用真 Git 確認 Candidate C 與 main M 的 ancestry 和 clean/conflict 分類。若 C 已含 M，probe 成功並允許既有 preflight、push 前重探測及授權 ship/push path 繼續；若 C 落後、衝突或 probe 失敗則 fail closed，不得進 preflight/push/建 PR/request Copilot。
- Probe 必須自行以 bounded subprocess 執行必要 Git calls，不使用會折疊錯誤 stage/returncode 的 `base_sha_probe`，也不使用沒有 timeout contract 的 default Git runner。每一階段（Candidate resolve/validate、fetch、FETCH_HEAD resolve/validate、merge-base、merge-tree、NUL path parse）都保留 typed stage、實際 `returncode`（timeout 時 null）、`error_kind`；每條子程序都有明確有限 timeout。fetch/rev-parse 未取得合法 M 時 `main_head=null`；取得合法 M 後的任何失敗都帶回同一 M。
- `merge-tree` 使用 `-z` NUL-delimited output parser，逐項保留完整 conflict paths；不得以換行、空白或一般字串 split 解析。舊 Git 不支援所需選項、非預期 return code、timeout、輸出格式錯誤都回 typed failure。
- C/M 需是完整且存在的 commit object ID，符合 repository object format；非法/縮寫 SHA、非 commit object 不可前進。
- CHANGELOG 只分類唯一 `[Unreleased]` 雙邊頂端插入；不在本票 merge、修檔或派 Builder。
- 整合 regression 必須證明 `fetch` failure 經 Manager 形成 `main-sync-unavailable` needs_human stop 後，修復 bare-origin 的環境條件，再從正式 operator workflow `resume` path 進入 ship validator、重新執行 bounded probe 並觀察新結果。此 child 驗證 stop/re-entry，不取代 child 04 的 durable nested-context read-back。測試必須確認不是 `work_actions._claim_action` 對非-define run 呼叫 `workflow_starter` 的 no-op 就算通過。
- 對應 #972 probe timing、classification、typed failure、fail-closed、merged/terminal skip；#943 真 merge、Builder re-dispatch、D reverify/push 保留給 #973。
