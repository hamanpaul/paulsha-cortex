---
status: accepted
work_item: porcelain-run-recover
---

# porcelain-run-recover Design

## Goals

用任務導向動詞（`run`/`recover`）收斂 `tick`/`fanout`/`complete`/`work`/`slice-action`/`reap-brokers` 六種底層操作語彙，並讓 mutation 一律顯性回報 `request_id`，同時不新增任何危險旁路旗標。

## Decisions

- **映射表而非分支發散**：`run`/`recover` 各自維護一個 dict，把使用者子命令詞映射到既有 request type 字串與參數轉換函式，避免 argparse 分支隨動詞數量發散。
- **復用 B2 的 request 顯性化與 wait**：`emit_request_accepted()`（UX §5 格式輸出）與 `--wait` 輪詢邏輯直接 import `paulsha_cortex/porcelain/request.py`，不重寫；若 B2 尚未落地，本批次以相同介面簽章先行實作於 `run`/`recover` 共用處，B2 落地後收斂為單一來源。
- **`recover service restart` 為別名**：直接呼叫 B4 `porcelain/service.py` 的 `restart` 函式，不重複實作 service+timer 成對邏輯。
- **危險旗標邊界**：argparse 定義中不存在 `--allow-unsafe` 等旁路參數，維持 UX 規格原則 4（專家需求退回低階命令）。
- **`--actor` fail-fast**：recover 家族的 argparse 對 `--actor` 設為必填，缺少時於用法錯誤層（exit 2）攔截，不進入映射邏輯。
