---
status: accepted
work_item: fix-service-install-overwrite
---

# fix-service-install-overwrite Design

## Decisions

### D1 idempotent guard

installer 安裝前讀取既有 `cortex-manager.env`（或 systemd unit）中記錄的 venv 路徑，與當前 `sys.executable` 推導的 venv 比較。若既有路徑有效（檔案存在且可執行）且指向不同 venv → 拒絕覆寫並 raise 清晰錯誤（含既有路徑、呼叫者路徑、建議操作）。若既有路徑無效或指向同一 venv → 正常安裝/更新。

### D2 錯誤訊息清晰

拒絕覆寫時錯誤訊息含：
- 既有 config 指向的 venv 路徑
- 當前呼叫者的 venv 路徑
- 建議操作（如「使用既有 venv 的 cortex 執行 install」或「先移除既有 config」）

### D3 不自動遷移

不自動修正既有 config（避免誤判）。僅偵測+拒絕+建議。

### 風險與 mitigation

- 既有 config 格式可能不一致 → guard 僅在可解析出有效 venv 路徑時生效，否則視為無效→正常安裝。
- 測試以 `tmp_path` 模擬兩個不同 venv 路徑與既有 config 檔。