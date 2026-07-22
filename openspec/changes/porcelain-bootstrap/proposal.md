---
status: accepted
work_item: porcelain-bootstrap
---

## Goals

讓使用者從一台乾淨機器開始，一個命令完成「環境檢查 → 服務安裝 → 啟動 → 健檢 → 下一步指引」，對齊研究報告「10 分鐘上手」KPI（issue #91）。

## Why

新使用者目前必須自行拼湊 `install service`、`systemctl start`、`doctor`、`status` 等多個命令才能完成第一次上手；deep research 的 P0/P1 摩擦與 dogfood F1（pipx 快照過期指向已刪 worktree）都指向「首次上手與重裝」缺乏單一入口。B5 以 `bootstrap` 收斂整個流程，並在環境不滿足時給出可執行指引而非單純報錯。

## What Changes

- 新增 `paulsha_cortex/porcelain/bootstrap.py`：`cortex bootstrap`（preflight、`--dry-run`、串接 B4 `service install/start`、B3 `inspect status/doctor`、選配 B7 `init-sample`、`--json`）。
- `porcelain._FAMILY_MODULES` 登記 bootstrap 模組。
- README 補「10 分鐘上手」段落並連結 bootstrap 命令（R-16）。

## Capabilities

### New Capabilities

- `porcelain-guided-bootstrap`: 從乾淨機器到第一次成功的導引式上手契約——preflight、service 安裝啟動、健檢、下一步指引一次到位，`--dry-run` 僅預覽。
