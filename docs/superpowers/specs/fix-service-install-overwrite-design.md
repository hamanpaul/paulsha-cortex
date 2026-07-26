---
status: accepted
work_item: fix-service-install-overwrite
---

# fix-service-install-overwrite Design

## Decisions

### D1 guard 邏輯

安裝前：
1. 讀取既有 `cortex-manager.env`（或 systemd unit `ExecStart`）中的 venv 路徑。
2. 判斷該路徑是否有效（`os.path.isfile` 且 `os.access(x, os.X_OK)`）。
3. 與 `sys.executable` 推導的 venv 比較。
4. 有效且不同 → raise `RuntimeError` 含兩路徑與建議。
5. 無效或相同 → 正常安裝。

### D2 不依賴 sys.executable 為唯一來源

guard 使 `sys.executable` 不再是無條件覆寫的來源。可考慮額外從已知路徑讀取（如 pipx 安裝路徑），但 guard 已足夠防止覆寫。

### 風險與 mitigation

- 既有 config 格式變異 → guard 僅在可解析有效路徑時生效。
- 首次安裝無既有 config → 直接安裝。