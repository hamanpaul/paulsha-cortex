# bootstrap code review

日期：2026-07-07

## 範圍

- Plan：`2026-07-07-cortex-repo-bootstrap-and-migration.md`
- 範圍：Task 1–10 的 bootstrap 實作與 Task 10 最終收尾

## 審查結論

### Strengths

- `pyproject.toml` 維持 `dependencies = []`，fresh install 可直接 `pip install .` 並執行 `cortex --help`
- `paulsha_hippo` runtime 依賴已清零；legacy deck import 只剩 `persona/loader.py`
- `README.md`、`CLAUDE.md`、systemd installer、runtime scripts、CI workflows 均已對齊 bootstrap 目標
- `tier: shareable` 去識別化掃描乾淨；policy 1.0.12 本機實跑為綠

### 第一輪發現與處置

1. `coordinator_telegram_notifier.py` 仍呼叫不存在的 `paths.home_root()` / `paths.max_root()`，且硬編外部 coordinator 腳本路徑  
   → 已改為直接讀 `jobs.json`，並以 `PSC_MAX_ROOT` / `Path.home()` 推導 token 路徑；新增 `tests/test_coordinator_telegram_notifier.py`
2. README / CLAUDE 仍含 legacy 主 repo literal  
   → 已清到只剩 `paulsha_cortex/persona/loader.py` 的 deck lazy import
3. fresh install 仍會因 `import yaml` 失敗  
   → 已新增 `paulsha_cortex/_yaml.py`，`loader.py` / `autonomy.py` 改走零依賴 parser；新增 `tests/test_zero_dependency_runtime.py`

### 第二輪發現與處置

1. smoke install 產生 `build/`、`*.egg-info/` 汙染 working tree  
   → 已刪除生成物，並在 `.gitignore` 補 `build/`、`*.egg-info/`

### Remaining Issues

- Critical：無
- Important：無
- Minor：無

## 風險與回歸

- 自製 YAML parser 僅支援目前 repo 需要的 subset（mapping、inline list、quoted scalar、簡單縮排）。現有 loader/frontmatter 測試已覆蓋本 repo 使用面，但若未來引入更複雜 YAML 語法，需先補測試再擴充 parser。
- `cortex` 目前沒有獨立的 service status 子命令；README 已明示應以 `systemctl --user status` 查詢。

## 驗證摘要

- `python -m pytest tests/ -q` → `270 passed, 3 subtests passed`
- `python3 -m policy_check --repo .` → `21 pass, 0 fail, 0 warn`
- fresh install smoke：`pip install .` 後 `cortex --help` exit `0`，`Requires:` 為空
- 去識別化掃描：`CLEAN`
- `grep -rn 'paulsha_hippo' paulsha_cortex/ | wc -l` → `0`

## Assessment

Ready for push / PR。bootstrap plan 的實作、零依賴、去識別化與 policy 1.0.12 本機驗證均已到位。
