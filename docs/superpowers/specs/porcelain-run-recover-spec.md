---
status: accepted
work_item: porcelain-run-recover
---

# porcelain-run-recover Specification

porcelain 計畫（epic #84）B6：`cortex run` 與 `cortex recover` 家族——高階工作語意（issue #92）。範圍以 `docs/superpowers/specs/2026-07-21-porcelain-cli-ux-design.md` §6.2、§6.6 與 §5 為準；`--wait` 復用 B2 `porcelain/request.py` 的 wait 輪詢基元。

## Requirements

### run 家族命令

`cortex run` SHALL 提供四個 mutation 子命令並映射既有 request types：`tick [--specs-dir D] [--executor E --model M] [--review-executor E --review-model M] [--wait]` → `tick` request；`fanout [...]`（參數同 tick，無 review）→ `fanout` request；`complete [--review-executor E --review-model M] [--wait]` → `complete` request；`work <start|resume|auto|ship|…> <work_id> --repo R [...] [--wait]` → `work-action` request。executor/model 省略時採 daemon 部署設定，輸出中 MUST 回顯實際生效值。

### recover 家族命令

`cortex recover` SHALL 提供四個 mutation 子命令並映射既有復原 primitives：`slice <slice_id> <retry-build|retry-verify|retry-review|abandon> --actor A [--wait]` → `slice-action`；`work <work_id> <retry-build|resume|abandon> --repo R [--wait]` → `work-action`；`brokers reap [--apply]` → `reap-brokers`；`service restart` → B4 `service restart` 別名。`--actor` MUST 為必填，不提供預設值（審計要求）。

### request_id 顯性化與 --wait

兩家族的 mutation 提交成功後 MUST 立即輸出 UX 規格 §5 格式的 `request_id`/`action`/`accepted`/`status`/`hint` 區塊。未帶 `--wait` MUST 以 exit 3 結束；帶 `--wait [--timeout N]`（預設 120s）MUST 復用 B2 request 模組的輪詢邏輯：成功 exit 0、terminal failure exit 1、timeout exit 3 並再次印出追蹤提示。

### 等價性與限制

`run`/`recover` 命令與其對應既有低階命令（`tick`/`fanout`/`complete`/`work`/`slice-action`/`reap-brokers`）在行為上 MUST 完全等價；既有低階命令 MUST 維持可用、行為不變。porcelain 層 MUST NOT 提供 `--allow-unsafe` 等危險旁路旗標。

### 輸出契約

預設人類可讀摘要；SHALL 支援 `--json`：run 家族 `"schema": "cortex-porcelain/run/v1"`，recover 家族 `"schema": "cortex-porcelain/recover/v1"`。

### 實作限制

以 B1 註冊表登記（`_FAMILY_MODULES` 加入 run 與 recover 兩模組）；stdlib-only；TDD；`test_zero_dependency_runtime` 續綠。
