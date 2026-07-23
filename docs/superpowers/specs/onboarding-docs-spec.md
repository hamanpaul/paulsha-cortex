---
status: accepted
work_item: onboarding-docs
---

# onboarding-docs Specification

porcelain 計畫（epic #84）B8：七份 onboarding 文件（issue #94）。範圍涵蓋 UX 規格 `docs/superpowers/specs/2026-07-21-porcelain-cli-ux-design.md` §6（七家族命令表）與 §9（名詞速查），並依賴 B3–B7（inspect/service/bootstrap/run-recover/init-sample）已凍結的命令面。

## Requirements

### 七份文件與範圍

SHALL 新增 `docs/onboarding/{quickstart,upgrade,rollback,troubleshooting,concepts,admin,runbook}.md` 七份文件：Quickstart（pipx install → `cortex bootstrap` → 第一個 workflow，目標 10 分鐘內完成）、Upgrade／Rollback（對齊 dogfood F1 pipx 快照過期路徑的升級/回滾步驟）、Troubleshooting（manager degraded、request timeout「F8」、systemd 不可用、executor 未登入、unit 指向過期 venv「F34」等故障對照）、Concepts（spec/job/slice/work 名詞關係，沿用 UX 規格 §9）、Admin（`service`/`inspect` 家族的日常維運操作）、Runbook（前述故障情境的操作手冊化 SOP）。

### 內容治理

每份文件的技術宣稱 MUST 可回溯到 UX 規格 §6 命令表、B3–B7 各批次輸出契約或既有 README/doctor 行為，MUST NOT 描述尚未落地的行為為既成事實；Concepts 文件的名詞定義 MUST 與 UX 規格 §9 一致，不得另造詞彙。

### 路徑衛生（R-21）

全七份文件與 README 導覽段 MUST 一律以 `~`、`$HOME`、環境變數或相對路徑表示路徑；MUST NOT 出現任何個人絕對路徑、使用者名或雇主／廠商識別（本 repo `tier: shareable`）。

### README 導覽

README SHALL 新增「新手上手」導覽段，連結七份 onboarding 文件。

### 驗收與限制

使用者 SHALL 能依 Quickstart 文件獨立完成第一個 workflow；文件 MUST 符合 R-18（docs 對齊）與 R-22（doc-reference 無新破壞）；不新增 runtime 依賴。
