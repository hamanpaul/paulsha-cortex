---
status: accepted
work_item: porcelain-bootstrap
---

# porcelain-bootstrap Specification

porcelain 計畫（epic #84）B5：`cortex bootstrap`——從乾淨機器到第一次成功（issue #91）。範圍以 `docs/superpowers/specs/2026-07-21-porcelain-cli-ux-design.md` §6.5 為準；依賴 B4 `porcelain-service` 已登記的 `service install`/`service start`。

## Requirements

### bootstrap 命令與流程

`cortex bootstrap [--instance I] [--repo-root P] [--interval N] [--start/--no-start] [--dry-run] [--sample COMBO --task "TEXT"] [--change NAME]` SHALL 依序執行：(1) 環境 preflight（Python/Git/repo-root/executor CLI 在 PATH 且已登入，只報狀態不代辦）(2) 呼叫 B4 `service install`（透傳 `--instance`/`--interval`）(3) `--start` 預設開，呼叫 B4 `service start`（systemd 不可用時說明 fallback）(4) 健檢：呼叫 B3 `inspect status` 與 `inspect doctor` 摘要 (5) 選配 `--sample`：呼叫 B7 `init-sample` (6) 輸出下一步指引。

### preflight 契約

環境不滿足（Python/Git 版本不符、非 git repo、executor CLI 不在 PATH 或未登入）MUST 以 exit 4 結束，且 MUST 列出「缺什麼、怎麼補」的可執行建議；bootstrap MUST NOT 代使用者登入 executor。

### --dry-run 契約

`--dry-run` MUST 只執行 preflight 檢查並預覽將呼叫的 `service install`/`service start` 參數，MUST NOT 產生任何 mutation（不呼叫 installer、不啟動服務）。

### sample 降級語意

`--sample` 失敗 MUST NOT 影響 bootstrap 核心步驟（preflight/install/start/健檢）的成功判定，但 MUST 在輸出中明確標示 sample 步驟的失敗狀態。

### 輸出契約

預設人類可讀摘要；SHALL 支援 `--json`（頂層含 `"schema": "cortex-porcelain/bootstrap/v1"`）。exit code 遵循 UX 規格 §3/§4：核心步驟失敗 exit 1，preflight 不滿足 exit 4。

### 限制

以 B1 註冊表登記；依賴 B3（`inspect`）與 B4（`service`）已登記的命令函式（非子行程呼叫既有 CLI，而是 import 對應模組）；stdlib-only；TDD（B3/B4 以 fake 家族模組 mock）；`test_zero_dependency_runtime` 續綠。
