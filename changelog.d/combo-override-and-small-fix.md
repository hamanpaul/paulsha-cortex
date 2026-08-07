### Added
- **Issue #324：combo 搜尋改支援 instance-local override，新增 small-fix 輕量 combo**：
  `paulsha_cortex/deck/schema.py` 新增 `resolve_combo_path()`／`iter_combo_files()`／
  `combo_search_dirs()`，一律先查 `$PSC_AGENTS_ROOT/config/combos/<id>.yaml`，找不到才
  fallback 到套件內建 `paulsha_cortex/deck/data/combos/`（同 id 時 instance-local
  優先、reinstall 不會蓋掉自訂檔）；`deck/selector.py`、`deck/cli.py`、
  `coordinator/work_bridge.py`、`porcelain/init_sample.py` 全數改走這兩個入口，
  `deck/cli.py` 保留自己的 `DEFAULT_COMBOS_DIR` module-level 繫結供既有測試
  monkeypatch 語意不受影響。另新增卡片 `writing-plans-light`（`cards.yaml`，只吃
  `docs/superpowers/specs/*<task-slug>*-design.md`，不依賴 openspec proposal）與
  參考 combo `small-fix`（`workflow-claim → brainstorming → writing-plans-light →
  subagent-build → verification → code-review → policy-commit`，7 張卡、2 條核心
  gate_spine，覆蓋 `WORKFLOW_PHASES` 各恰一張），打斷小任務不需要的 openspec
  requires 全鏈；`small-fix` 只能經 `--combo small-fix` explicit override 使用，
  不進 `task-types.yaml` 自動選牌映射。`docs/unified-work-lifecycle.md` 補上兩者
  說明。
