---
status: accepted
work_item: porcelain-run-recover
---

# porcelain-run-recover Plan

## Tasks

### 1. TDD RED

- [ ] 新增 `tests/test_porcelain_run.py`：`run tick/fanout/complete/work` 對應底層 request type 與參數轉換正確性、executor/model 省略時回顯部署設定、`--wait`/未 `--wait` 退出碼 0/1/3、request_id 輸出格式、`--json` schema `cortex-porcelain/run/v1`；先確認 RED。
- [ ] 新增 `tests/test_porcelain_recover.py`：`recover slice/work/brokers reap/service restart` 對應底層 primitive、`--actor` 缺少時 exit 2、`--wait` 行為、`--json` schema `cortex-porcelain/recover/v1`、確認 argparse 無 `--allow-unsafe` 等旁路旗標；先確認 RED。

### 2. 實作

- [ ] `paulsha_cortex/porcelain/run.py`：`run tick/fanout/complete/work` 四子命令，映射既有 request types。
- [ ] `paulsha_cortex/porcelain/recover.py`：`recover slice/work/brokers/service` 四子命令，映射既有復原 primitives；`service restart` 呼叫 B4 `service.restart`。
- [ ] `paulsha_cortex/porcelain/__init__.py`：`_FAMILY_MODULES` 加入 run 與 recover 兩模組。

### 3. 同步與驗證

- [ ] README 的 CLI 命令面補 `run`／`recover` 兩家族段落（R-16）。
- [ ] 新增 `changelog.d/porcelain-run-recover.md` fragment，`CHANGELOG.md [Unreleased]` `### Added` 加入含 `porcelain-run-recover` 字樣的條目。
- [ ] `python3 -m pytest tests/ -q` 全綠；`python3 -m policy_check --repo .` 0 fail；`git diff --check` 乾淨。
- [ ] 完成後勾選 `openspec/changes/porcelain-run-recover/tasks.md` 對應項並以 conventional commit 提交（不得改動本 plan 檔）。
