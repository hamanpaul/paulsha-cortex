---
status: accepted
work_item: fix-service-install-overwrite
---

## Goals

修正 `deploy/installer.py` 以 `sys.executable` 推導路徑，使其他程式（如 paulshaclaw）內嵌舊版 cortex 時 `install service` 覆寫正確 config 為舊 venv 路徑。

## Why

`#148` 回報：`paulsha_cortex/deploy/installer.py` 用 `sys.executable` 推導路徑。當另一程式（paulshaclaw）內嵌舊版 cortex 在其 venv 並呼叫 `install service`，installer 用呼叫者的 `sys.executable` render `cortex-manager.env` 與 systemd unit，覆寫正確路徑為舊 venv 路徑。覆寫為潛伏（不重啟無感）直到下次 `systemctl restart`。

## What Changes

- `paulsha_cortex/deploy/installer.py`：
  - 安裝前偵測既有 config 是否指向不同（有效）venv——idempotent guard，拒絕覆寫。
  - 或鎖定/安裝到已知路徑，而非從呼叫者 `sys.executable` 推導。
- `tests/`：回歸測試——既有有效 config + 不同呼叫者 venv → 不覆寫。

## Capabilities

### Modified Capabilities

- `deploy-installer`：installer 含 idempotent guard，偵測既有有效 config 指向不同 venv 時拒絕覆寫。