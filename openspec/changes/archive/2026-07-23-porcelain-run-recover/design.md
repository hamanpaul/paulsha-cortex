---
status: accepted
work_item: porcelain-run-recover
---

# Design

## Decisions

- **映射表而非分支發散**：`run`/`recover` 各維護命令詞 → request type/action 的映射 dict。
- **復用 B2 request 顯性化與 wait 邏輯**：`emit_request_accepted()` 與輪詢函式 import 自 `porcelain/request.py`。
- **`recover service restart` 為 B4 別名**：直接呼叫 `service.restart`，不重複實作。
- **`--actor` fail-fast**：recover 家族必填，argparse 層攔截缺少情境。
- **不定義危險旁路旗標**：維持 porcelain 危險旗標不外露原則。
