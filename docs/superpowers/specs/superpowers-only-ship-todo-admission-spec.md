---
status: draft
work_item: superpowers-only-ship-todo-admission
---

# Superpowers-only 工作的 ship Todo admission 與既有 run 恢復規格草案（#1051）

## Requirements

### R1 — 唯一 Todo source 必須在 build 前成立

Manager MUST 在第一次派發 builder job 前確認當前 `WorkAuthority` 恰有一個 `mapped_todo_paths`。沒有 Todo source 時，run MUST 在 planning／build admission 邊界以穩定且可辨識的 `missing-todo-source` 原因停止；有多個 Todo source 時 MUST 以 `ambiguous-todo-source` 停止。兩種情形都不得建立或啟動 builder job、產生候選、push 或建立 PR。ship 的既有 gate 保留為防禦性 backstop。

`mapped_openspec` 可以為空或恰有一筆。#911 已讓「沒有 OpenSpec」成為合法交付模式；本規格不得把 OpenSpec 數量重新要求為恰好一筆。PR 的唯一性仍在 ship admission 檢查，不能要求 build 前已有 PR。

### R2 — Superpowers plan 不會隱式成為交付 Todo

Accepted Superpowers spec、design、plan 是規劃 authority；它們本身不構成 `mapped_todo_paths`。不得只憑檔名、checkbox、規劃完成狀態或已存在的 `planning_authority` 將 plan 推升成 Todo source。此草案選擇要求一個獨立且由 `WorkAuthority` 確認的 Todo path；在該 source 未成立前，Superpowers-only 工作停在 build admission。

若未來要讓 Manager 把 accepted plan 轉成 Todo，該轉換必須另定可驗證的 owner、來源 ref／revision、輸出 hash、持久化位置和 CAS 契約，並證明 generated path 會由 Monitor 正式納入 `WorkAuthority`。在該契約被審核前不得採用隱式轉換。現行修復路徑是由 owner 發布有 issue provenance、matching `work_item` metadata 與具體 tasks 的 canonical `docs/superpowers/workstreams/<slug>/todo.md`，再用 `cortex work link <work_id> --repo <owner/repo> --kind path --ref <repo-relative-todo-path>` link 已存在 path；CLI link 本身不會創造 Todo，WorkAuthority 必須等 Monitor correlation 的 fresh snapshot 確認。

### R3 — 缺少 Todo 的 diagnostic 指出真正缺口

對未經新 build gate 的既有 run，ship 仍須在任何外部交付副作用之前 fail closed。當 `mapped_todo_paths=0` 時，diagnostic MUST 明說缺少唯一的已確認 Todo source。對尚未開工的新 work，提示 owner 建立具 provenance 的 canonical workstream Todo、link 已存在 source path、等待 fresh Monitor snapshot，再由正式 start/intake gate 判定。對已有 run/candidate/PR，不得提示直接 resume/re-intake；須交由 Recovery child 凍結的 CAS/evidence transition。任何情境都不得只建議 `unlink`；`unlink` 只適用於多餘來源；`mapped_todo_paths>1` 的診斷可以建議移除多餘 mapping。

### R4 — 已有 exact Candidate 與 PR 的 run 必須有正式恢復契約

對 #983 類 run，恢復 MUST 先確認 exact run id、claim key、來源 revisions、WorkAuthority digest、candidate／verified head、既有 verify／review proofs、PR number／head 與 delivery binding。新增 Todo source 必須由 WorkAuthority owner 透過受支援的 source link 與 Monitor snapshot refresh 採信；不得手改 registry、delivery journal、run claim key 或 source revision。

恢復契約 MUST 明定 authority／claim 的 CAS、每類舊證據是否保留或失效，以及 Manager 對既有 run resume 或新 run restart 的唯一合法流程。因 authority 變動而不再符合 exact binding 的證據不得被假定仍有效。無法證明 transition 完整且唯一時，MUST fail closed 並給明確的人工作業出口。

對有既存 PR 的恢復，驗收 MUST 證明不會再次 push、建立第二個 PR 或 merge；如果某路徑要求重做 verify／review，必須綁定同一個 exact candidate head。PR #1049 的 main conflict 屬 #972／#973，不能在本 work item 內改寫或處置。

### R5 — 測試保留所有授權邊界與六種情境

測試 MUST 覆蓋 Superpowers-only、無 OpenSpec、有唯一 Todo、缺 Todo、偽造 path link、以及已有 PR 的停機恢復。它們 MUST 驗證 missing／ambiguous Todo 在 builder dispatch 前停止、path 僅在 WorkAuthority 正式確認後可用、claim／evidence 的 CAS 與失效範圍正確，並確認不會重複 push／PR／merge。測試不得連線到真 GitHub、改動正式 registry／journal 或使用真實 issue run。

## Child allocation

- **#1054** owns R1–R3 and the new-work admission cases: Manager admission/diagnostic consumes WorkAuthority/Monitor as source of truth; no dependency on the recovery work.
- **#1055** owns R4 and the existing-candidate/PR recovery case: Manager recovery works with claim/evidence/delivery owners and GitHub PR facts. It is **Blocked by #1054** and consumes the admission/diagnostic contract delivered there.
- Both child issues remain separate implementation authorities under parent #1051. This draft stays `status: draft`, 10/Red; creating the child issues does not accept or intake this packet.

## Authority boundaries

- Manager 擁有 intake、planning/build admission、ship admission 及受支援的 run transition。
- WorkAuthority 擁有已確認的 issue／PR／Todo／OpenSpec mapping 與來源 revision；`.cortex/work-items.yaml` override 必須經 Monitor correlation 進入 snapshot 後才是可採信 authority。
- GitHub 提供 issue、PR、head、merge 與 review/check 的遠端事實。
- Superpowers artifacts 提供 planning authority，不自行取得 WorkAuthority 的 Todo mapping。
- 此規格不授權手改 workflow registry、delivery journal、claim key、source revision，或操作正式 run／PR。

## Acceptance

- 在唯一 Todo source 不存在或有歧義時，build job 數量為零；已有唯一 confirmed mapping 時才可進入 build admission。
- Superpowers plan 不會因為被 accepted 或包含 checkbox 而自動計入 `mapped_todo_paths`；若沒有獨立 confirmed Todo，reason 與 next action 清楚且不建議只做 `unlink`。
- `mapped_openspec=0` 與 #911 的合法路徑相容；#810 的「merge 後 checkbox 完成度」維持另一個問題，不由此規格變更。
- #983 型恢復只有在 exact identities、CAS 與證據失效範圍均通過時才可繼續，且不重複外部 side effect；否則走明確的 fail-closed 出口。
- 完成 R5 列出的完整測試矩陣。
