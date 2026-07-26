---
status: accepted
work_item: fix-service-install-overwrite
---

# fix-service-install-overwrite Specification

`#148`：修正 installer 以 `sys.executable` 推導路徑，使其他程式內嵌舊版 cortex 時覆寫正確 config。

## Requirements

### R1 idempotent guard

`paulsha_cortex/deploy/installer.py` MUST 在安裝前偵測既有 config（`cortex-manager.env` / systemd unit）指向的 venv 路徑。若既有路徑有效（檔案存在且可執行）且指向不同 venv → MUST 拒絕覆寫並 raise 清晰錯誤。

### R2 正常安裝路徑

既有 config 無效、不存在、或指向同一 venv 時 → MUST 正常安裝/更新（idempotent）。

### R3 錯誤訊息

拒絕覆寫時錯誤訊息 MUST 含既有 venv 路徑、當前呼叫者 venv 路徑、建議操作。

### R4 限制

- stdlib-only；TDD。
- 不得改變既有對外 CLI envelope schema。
- `test_zero_dependency_runtime` 續綠；`python3 -m policy_check --repo .` 0 fail。