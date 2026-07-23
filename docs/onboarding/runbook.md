# Runbook

這份文件把常見事故整理成可操作的 SOP。每一條都用同一個節奏：偵測、收斂、修復、驗證。

## 引用來源

- `docs/superpowers/specs/onboarding-docs-spec.md`
- `docs/superpowers/specs/porcelain-request-spec.md`
- `docs/superpowers/specs/porcelain-service-spec.md`
- `docs/superpowers/specs/porcelain-inspect-spec.md`
- issue #94
- dogfood findings: F8、F34
- `python3 -m paulsha_cortex.cli request --help`
- `python3 -m paulsha_cortex.cli service --help`
- `python3 -m paulsha_cortex.cli inspect --help`

## Incident 1: manager degraded

### 偵測

```bash
cortex status
cortex service status --instance cortex --json
cortex inspect service --instance cortex --json
```

### 收斂

- 記下目前的 request / job 是否仍在跑
- 不要立刻重送新的 `tick` 或 `complete`

### 修復

1. 若 service 沒起來，先：

```bash
cortex service restart --instance cortex
```

2. 若 `inspect service` 顯示 stale `venv`、exec path drift 或 F34 類型異常，改走 [Upgrade](upgrade.md) 或 [Rollback](rollback.md)。

### 驗證

```bash
cortex service status --instance cortex --json
cortex inspect service --instance cortex --json
cortex status
```

## Incident 2: request timeout

### 偵測

CLI 在 `--wait` 路徑超時，或使用者看到 request timeout。這對應 dogfood F8 類型問題。

### 收斂

```bash
cortex request list
cortex request show <request-id>
cortex request logs <request-id>
```

### 修復

- 如果 request 還在進行中，先：

```bash
cortex request wait <request-id> --timeout 30
```

- 再看是否真的需要 recovery：

```bash
cortex jobs
cortex status
```

### 驗證

- request 進入 terminal
- 沒有重複派出第二個同類 mutation
- 若 workflow 已成功推進，狀態與 job 數量和預期一致

## Incident 3: systemd 不可用

### 偵測

```bash
cortex service status --instance cortex --json
systemctl --user status
```

### 收斂

- 先確認這台機器是否本來就不打算用 `systemd --user`
- 不要假設所有部署都必須長得一樣

### 修復

- 若這台機器應支援 `systemd --user`，修正使用者 session / unit 安裝後再重試
- 若這台機器就是非 `systemd` 環境，改用 `cortex service status` 與 `cortex service logs` 先確認目前 runtime 真相

### 驗證

- service mode 與你的部署預期一致
- 之後的 bootstrap / start / restart 指令都回到可預期路徑

## Incident 4: executor 未登入

### 偵測

```bash
cortex doctor --probe-live --repo owner/name --json
```

### 收斂

- 記錄是哪一個 executor 失敗：`copilot`、`claude` 或 `codex`
- 先不要重啟 manager

### 修復

- 依該 executor 的官方登入流程完成登入
- 重新執行 bootstrap 或原本的 request

### 驗證

- `cortex doctor --probe-live ...` 不再回報 executor 問題
- workflow 可以正常建立 job

## Incident 5: stale venv / F34

### 偵測

```bash
cortex inspect service --instance cortex --json
```

關鍵訊號：

- unit 指向不存在的 `venv`
- service 版本與 `cortex --version` 不一致
- service 看似存活，但行為仍停在舊版

### 收斂

- 暫停新的 workflow mutation
- 記錄目前版本、exec path、`venv` 狀態

### 修復

```bash
pipx install --force git+https://github.com/hamanpaul/paulsha-cortex.git
cortex service restart --instance cortex
```

若新版本本身有問題，改用 [Rollback](rollback.md) 的 known-good ref 安裝方式。

### 驗證

```bash
cortex --version
cortex service status --instance cortex --json
cortex inspect service --instance cortex --json
```

直到 `venv` 與 exec path 漂移消失，才恢復正常派工。
