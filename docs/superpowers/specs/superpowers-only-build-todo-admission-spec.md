---
status: accepted
work_item: superpowers-only-build-todo-admission
issue: 1054
---

# Superpowers-only 工作在 Builder 前的唯一 Todo admission 規格（#1054）

## Requirements

### R1 — 第一個 Builder 派工前必須有唯一 canonical Todo

Manager MUST 在 plan→build 的第一個 Builder 派工前，從當前成功關聯的 Monitor snapshot 載入該 `(repo, work_id)` 的 WorkAuthority，且確認 `mapped_todo_paths` 恰有一個來源。零個來源以 `missing-canonical-todo-source` typed admission stop 停止；多個來源以 `ambiguous-canonical-todo-source` typed admission stop 停止。若 WorkAuthority 缺席、無法解析、snapshot 尚未反映 source link 或 Monitor refresh 失敗，必須 fail closed，不得把目前 `.cortex/work-items.yaml` 的 override 當成已確認 authority。

Stop 必須發生在任何第一個 Builder side effect 之前：不得 reserve/create Builder job、建立 Builder worktree 或 spawn agent。唯一 confirmed Todo 才可放行第一張 Builder card。後續卡片沿用現有 run/claim authority 規則；此需求不新增另一個 claim 世代或 durable authority store。

### R2 — Todo 必須是具 provenance 的 owner-published canonical source

唯一可計數的 Todo 是 owner 發布且既存於 repo 的 `docs/superpowers/workstreams/<slug>/todo.md`。該 source MUST 有指向 work item 所屬 issue 的 `issue` provenance、與 WorkAuthority `work_id` 相符的 `work_item` metadata，以及具體、可執行的 `## Tasks` 清單。它必須通過既有 canonical path parser、symlink/path guard 與 Monitor scanner；Superpowers spec/design/plan、檔名 `todo.md`、checkbox、已 accepted 的 planning authority 或單獨的 path override 都不能代替 confirmed Todo。

`cortex work link <work_id> --repo <owner/repo> --kind path --ref <repo-relative-todo-path>` 只能關聯已存在且通過 scanner 的 source。link 寫入 override 不會建立 Todo、改寫 source revision 或使 WorkAuthority 立即更新。Manager 只信任之後 fresh Monitor correlation snapshot 產生的 WorkAuthority 與其 snapshot/source revision；不可從 override、run planning artifacts 或 Git 工作樹自行推導一筆映射。

### R3 — 零／多個映射都要有可執行且不誤導的診斷

兩種拒絕都必須以現有 typed `DiagnosticReason` 形式記錄，保留穩定 reason、Manager source、run/work identity、數量、可讀 authority reference，以及可直接執行的 `next_step_hint`。zero 的 reason 是 `missing-canonical-todo-source`；提示 owner 先發布具 issue provenance、matching `work_item` 與具體 tasks 的 canonical Todo，再 link 已存在 path、等待 fresh Monitor snapshot，最後回到正式 Manager `start`／`intake` admission。zero 診斷不得建議 `unlink`。

multiple 的 reason 是 `ambiguous-canonical-todo-source`；提示目前映射數與具體多餘 path，要求操作者只移除多餘 mapping（例如用 `cortex work unlink <work_id> --repo <owner/repo> --kind path --ref <repo-relative-extra-path>`），等待 fresh Monitor snapshot 後再走正式 Manager admission。兩種 next action 都不得把手改 override、直接 dispatch 或沒有 fresh snapshot 的狀態描述成已修復。

### R4 — 保留既有 ship、OpenSpec 與相關 issue 邊界

`work_actions._ship_action` 的 PR=1、Todo=1、OpenSpec=0 或 1 backstop MUST 保留；build 前 gate 不得放寬 ship 數量條件。`mapped_openspec=0` 仍是 #911 支援的合法 ship lane；不得要求每個 Superpowers-only work item 另造 OpenSpec change。

本規格只阻止尚未建立第一個 Builder job 的 plan→build 轉換。已有 Candidate／verify/review evidence／PR 的 claim-era CAS、恢復或冪等交付仍屬 #1051 的 recovery 子票，不能在 #1054 改動 run registry、claim key、delivery journal、既有 PR 或 evidence。#810 的 merge 後 Todo checkbox closure 以及 #972/#973 的 PR #1049 main conflict 各自獨立，不得以本 admission gate 取代或擴張其範圍。

### R5 — 以真實 Manager 與 WorkAuthority fixture 驗證 side-effect boundary

測試 MUST 使用真實 Manager plan→build dispatch seam 與 WorkAuthority loading/correlation fixture，覆蓋 Superpowers-only、無 OpenSpec、唯一 Todo、缺 Todo、多 Todo、偽造或未反映的 path link、以及 Monitor refresh 不可用。必須證明零／多個或 snapshot 不可信時，Builder job、Builder worktree、agent dispatch 三者都為零；只有唯一、canonical、freshly confirmed Todo 可進第一個 Builder side effect。

source fixture 必須包含 issue provenance、matching `work_item` 與具體 tasks，並通過 repo 既有 canonical parser 和 path guard。測試應驗證 `link --kind path` 對不存在／不合格 source fail closed，寫入 override 後但 fresh snapshot 未反映前仍不放行；fresh snapshot 收錄唯一 source 後才可放行。不得連接真 GitHub、改正式 Monitor snapshot、操作真 run 或 PR。

## Authority boundaries

- Manager 擁有 plan→build admission、typed stop 與 operator next action。
- WorkAuthority／Monitor snapshot 是已確認 Todo mapping、source revision、snapshot freshness 的唯一事實來源；Manager 不創建或自行推導 source authority。
- owner 發布 canonical workstream Todo，`cortex work link --kind path` 只關聯既有 scanner source；Monitor fresh correlation 後才更新 confirmed WorkAuthority。
- GitHub issue 提供 issue identity/provenance；issue number 不代表 Todo mapping 已存在。
- Superpowers spec/design/plan 提供 planning authority，不會隱式成為交付 Todo。

## Acceptance

- 第一個 Builder dispatch 前恰有一個 confirmed canonical `mapped_todo_paths`；0／多個均產生 typed admission stop，且 Builder job、worktree、agent dispatch 數都是零。
- 只連結已存在且通過 canonical parser／symlink/path guard 的 source；新 override 必須等 fresh Monitor snapshot 確認後才可用。
- zero 診斷明示真實缺口、數量、authority reference 及 owner publish→link existing path→fresh snapshot→formal start/intake 的 next action，不建議 unlink；multiple 診斷指出多餘 path 與可執行 unlink 修復。
- ship 唯一 Todo backstop、#911 的無 OpenSpec 支援、#810 checkbox closure、#972/#973 PR conflict 與 #983 已有 Candidate／PR recovery 邊界都保持獨立。
