---
status: accepted
work_item: porcelain-bootstrap
---

# Design

## Decisions

- **import 依賴而非 shell out**：直接呼叫 B4 `service.install`/`service.start` 與 B3 `inspect.status_summary`/`inspect.doctor_summary` 函式。
- **落地順序假設 B3→B4→B5**：先行落地時以 fake 模組驗證介面契約，介面簽章於本 design 凍結。
- **preflight 四項固定檢查**：Python/Git/repo-root/executor CLI 登入態，每項失敗附具體修法。
- **sample 非阻斷降級**：`--sample` 失敗只反映於輸出欄位，不影響整體 exit code。
