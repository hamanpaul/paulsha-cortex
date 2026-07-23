---
status: accepted
work_item: porcelain-bootstrap
---

# porcelain-bootstrap Design

## Goals

把「環境檢查 → 服務安裝 → 啟動 → 健檢 → 下一步指引」收斂成單一命令，對齊研究報告「10 分鐘上手」KPI（issue #91），且不重新實作 B3/B4 已提供的能力。

## Decisions

- **直接 import 依賴，不 shell out**：`bootstrap` import B4 `porcelain/service.py` 的 `install`/`start` 函式與 B3 `porcelain/inspect.py` 的 `status`/`doctor` 函式，而非以子行程呼叫 `cortex service install` 字串命令，降低耦合與測試成本。
- **落地順序假設**：本批次假設 B3→B4→B5 依序落地；若 B5 先行實作，測試以 fake `service`/`inspect` 模組先驗證介面契約，待 B3/B4 落地後移除 fake、接上真實模組（介面簽章在本 design 先凍結：`service.install(args) -> ServiceResult`、`inspect.status_summary() -> dict`）。
- **preflight 檢查表固定四項**：Python 版本、`git --version`、repo-root 為 git repo、至少一個 executor CLI（`copilot`/`claude`/`codex`）在 PATH 且以該 CLI 的 whoami/status 探測登入態；每項失敗附具體修法（例如指向該 executor 官方登入指令的提示）。
- **sample 降級為非阻斷**：`--sample` 的失敗只反映在輸出的 `sample` 欄位，不改變 bootstrap 整體 exit code（除非核心步驟本身也失敗）。
