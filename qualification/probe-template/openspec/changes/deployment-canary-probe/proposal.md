## Why

Deployment canary 需要一個小而可 merge 的行為變更，驗證 Cortex 從 intake 到 ship 的完整路徑。

## What Changes

- `normalize_label` 對全空白標籤回傳 `unnamed`。

## Impact

- 影響程式：`src/canary_probe.py`、`tests/test_canary_probe.py`。
