---
status: accepted
work_item: porcelain-inspect
---

# porcelain-inspect Specification

porcelain 計畫（epic #84）B3：`cortex inspect` 家族——統一唯讀檢視面（issue #89）。範圍以 `docs/superpowers/specs/2026-07-21-porcelain-cli-ux-design.md` §6.3 為準。

## Requirements

### inspect 家族命令

`cortex inspect` SHALL 提供六個唯讀子命令：`status`（包裝 control `status.json`）、`job <job_id>`（包裝 `stat`）、`ready`（包裝 `ready`）、`work <work_id> [--repo R] [--explain]`（包裝 Monitor socket `work show`）、`doctor [--repo R]`（包裝 `doctor`）、`service`（服務模式與運行時檢視）。`inspect <name>` 為薄包裝層，既有低階命令行為不變。

### 資料來源與唯讀性

`status`/`ready`/`job` MUST 直讀 control root 的 `status.json` 與 job registry 檔案，不經 control queue；`work` MUST 透過既有 Monitor Unix socket 的 `_work_read_main` 基元讀取；`doctor` MUST 包裝既有 `doctor.py` 邏輯。全家族 MUST NOT 產生任何 mutation、MUST NOT 寫入狀態、MUST NOT 經 control queue 提交請求。

### inspect service 的運行時真相

`inspect service` MUST 顯示服務運行模式（`systemd` / `fallback` / `none`）、unit 狀態、pid，以及運行中程式的 exec path 與版本；當 systemd unit 的 `ExecStart` 指向的 venv 路徑已不存在時 MUST 標示為潛在殭屍行程（canary F34 實證需求；呼應 dogfood F1「pipx 快照過期指向已刪 worktree」與 F3「舊 daemon 長期跑過期碼而無人察覺」）。

### 輸出契約與涵蓋範圍

預設人類可讀摘要；全子命令 SHALL 支援 `--json`（頂層含 `"schema": "cortex-porcelain/inspect/v1"`，snake_case，UTC ISO-8601）。同一查詢對象的人類輸出與 `--json` 輸出 MUST 內容一致、可互相對照（issue #89 驗收條件）。exit code 遵循 UX 規格 §3；`job`/`work` 查無對象時 exit 1 並附下一步建議命令。六子命令 MUST 涵蓋既有 `status`/`jobs`/`stat`/`list`/`work show`/`doctor` 的查詢能力，無功能倒退。

### 限制

以 B1 註冊表登記（`_FAMILY_MODULES` 加入 inspect 模組）；stdlib-only；TDD（tmp control root + fake job registry + fake systemd unit fixtures）；`test_zero_dependency_runtime` 續綠。
