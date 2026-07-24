# Admin

這份文件整理日常維運時最常用的命令，目標是先用 `service` / `inspect` / `request` 家族看真相，再決定是否需要 recovery。

## 引用來源

- `docs/superpowers/specs/2026-07-21-porcelain-cli-ux-design.md` §6
- `docs/superpowers/specs/onboarding-docs-spec.md`
- `docs/superpowers/specs/porcelain-service-spec.md`
- `docs/superpowers/specs/porcelain-inspect-spec.md`
- `docs/superpowers/specs/porcelain-request-spec.md`
- issue #94
- `python3 -m paulsha_cortex.cli service --help`
- `python3 -m paulsha_cortex.cli inspect --help`
- `python3 -m paulsha_cortex.cli request --help`

## 日常檢查

```bash
cortex service status --instance cortex --json
cortex inspect service --instance cortex --json
cortex inspect doctor --json
cortex status
cortex list --repo owner/name --state on-going --explain
```

重點：

- `cortex service ...`：看 service/runtime/logs 與 start/stop/restart
- `cortex inspect ...`：唯讀查詢 status/job/ready/work/doctor/service
- `cortex status`：看 manager 目前 gate 與 slice 狀態
- `cortex list`：看跨來源投影出的 work read model

## 常用操作

### service

```bash
cortex service start --instance cortex
cortex service restart --instance cortex
cortex service logs --instance cortex -n 50
cortex service uninstall --instance cortex --json
```

### inspect

```bash
cortex inspect ready --json
cortex inspect work <work-id> --repo owner/name --json
cortex inspect job <job-id>
```

### request

```bash
cortex request list
cortex request show <request-id>
cortex request wait <request-id> --timeout 30
```

## 建議的日常節奏

1. 先看 `cortex service status --json`，確認 manager 在不在、跑在哪個 mode。
2. 再看 `cortex inspect service --json`，確認執行中的版本與 `venv` 沒漂移。
3. 有 mutation 剛送出時，看 `cortex request ...`，不要只盯著終端機是否 timeout。
4. 要查跨 repo 工作面時，改看 `cortex list` / `cortex work show`。

## 什麼情況要升級或回滾

- `inspect service` 抓到 stale `venv`
- service restart 後仍持續 `manager degraded`
- 同一版 CLI 反覆出現 request timeout、或行為與 release note 不一致

這時先轉去 [Upgrade](upgrade.md) 或 [Rollback](rollback.md)，不要把日常維運操作硬當成修復流程。
