---
status: accepted
work_item: porcelain-service
---

# porcelain-service Specification

porcelain 計畫（epic #84）B4：`cortex service` 家族——服務生命週期管理（issue #90）。範圍以 `docs/superpowers/specs/2026-07-21-porcelain-cli-ux-design.md` §6.4 為準。

## Requirements

### service 家族命令

`cortex service` SHALL 提供七個子命令：`install [--instance I] [--repo-root P] [--interval N]`（包裝既有 deploy installer：render/copy units、`daemon-reload`、enable，不 start）、`start`/`stop`/`restart`（systemd 模式下 service+timer 成對操作；fallback 模式管理 daemon 行程）、`status`（模式、unit 狀態、pid、運行版本、env 摘要）、`logs [--follow] [-n N]`（systemd 走 `journalctl`、fallback 走 log 檔 tail）、`uninstall [--purge]`（停用並移除 units，`--purge` 才清 runtime env）。

### 三模式契約

全家族 MUST 明確區分 `systemd`／`fallback`／`未安裝` 三種模式並在輸出中標示；systemd 不可用時 MUST 說明並指出 fallback 路徑，MUST NOT 回報假成功。

### service+timer 成對操作

`start`/`stop`/`restart` 在 systemd 模式下 MUST 同時操作 `<instance>.service` 與 `<instance>.timer` 兩個 unit；MUST NOT 出現「只停 service、timer 仍週期喚醒」的不一致（呼應 dogfood F2「daemon env 與 CLI 旗標雙軌」、F3「舊 daemon 長期跑過期碼無人察覺」）。

### status 的運行時真相

`service status` MUST 顯示模式、unit 狀態、pid、運行中版本與 exec path drift 偵測，復用 B3 `porcelain/_runtime_probe.py` 的 `probe_service_runtime()`；MUST 顯示 env 摘要（executor/interval/specs-dir）。

### 輸出契約

預設人類可讀摘要；全子命令 SHALL 支援 `--json`（頂層含 `"schema": "cortex-porcelain/service/v1"`）。exit code 遵循 UX 規格 §3；環境不滿足（如 systemd 不可用且無 fallback）exit 4。

### 限制

以 B1 註冊表登記；stdlib-only；TDD（tmp systemd unit / journalctl mock fixtures）；`test_zero_dependency_runtime` 續綠。
