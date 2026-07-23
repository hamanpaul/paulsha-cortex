# Troubleshooting

這份文件用來快速對照常見故障，先判斷是哪一類問題，再決定要進一步看哪個 SOP。

## 引用來源

- `docs/superpowers/specs/2026-07-21-porcelain-cli-ux-design.md` §6
- `docs/superpowers/specs/onboarding-docs-spec.md`
- `docs/superpowers/specs/porcelain-request-spec.md`
- `docs/superpowers/specs/porcelain-service-spec.md`
- `docs/superpowers/specs/porcelain-inspect-spec.md`
- issue #94
- dogfood findings: F8、F34
- `python3 -m paulsha_cortex.cli request --help`
- `python3 -m paulsha_cortex.cli service --help`
- `python3 -m paulsha_cortex.cli inspect --help`

## 快速對照

| 症狀 | 先看哪個命令 | 常見原因 | 下一步 |
| --- | --- | --- | --- |
| `manager degraded` | `cortex status` | service 沒起來、runtime 漂移、權限或設定問題 | 看 [manager degraded](#manager-degraded) |
| request timeout / F8 | `cortex request list` | CLI 等待視窗結束，但 manager 還在背景處理 | 看 [request timeout](#request-timeout-f8) |
| `systemd` 不可用 | `cortex service status --json` | 在沒有 `systemd --user` 的環境操作，或 unit 未安裝 | 看 [systemd 不可用](#systemd-不可用) |
| executor 未登入 | `cortex doctor --probe-live --repo owner/name --json` | `copilot`、`claude`、`codex` 尚未登入 | 看 [executor 未登入](#executor-未登入) |
| F34 / stale `venv` | `cortex inspect service --json` | unit 指向已刪的安裝位置，或 service 還在跑舊碼 | 看 [stale venv 或 exec path drift](#stale-venv-或-exec-path-drift-f34) |

## manager degraded

先查整體狀態：

```bash
cortex status
cortex service status --instance cortex --json
cortex inspect service --instance cortex --json
```

如果 `service status` 顯示 unit 沒起來，先重啟：

```bash
cortex service restart --instance cortex
```

如果 `inspect service` 顯示執行中的 `venv` 或 exec path 不存在，這通常不是單純重啟能解的問題，直接轉去 [Upgrade](upgrade.md) 或 [Rollback](rollback.md)。

## request timeout (F8)

`cortex run tick --wait`、`cortex run complete --wait` 或其他 mutation request 超時時，先記住一件事：timeout 不等於失敗。

先查 request：

```bash
cortex request list
cortex request show <request-id>
cortex request logs <request-id>
```

若 request 還沒 terminal，再多等一小段：

```bash
cortex request wait <request-id> --timeout 30
```

接著再看：

```bash
cortex jobs
cortex status
```

F8 的判讀原則是「先查 request / job 真相，再決定要不要 retry」，不要直接重送同一個 mutation。

## systemd 不可用

先查目前 service mode：

```bash
cortex service status --instance cortex --json
```

如果你本來預期用 `systemd --user`，先確認：

```bash
systemctl --user status
```

當前環境若不支援 `systemd --user`，要先決定這是不是預期部署方式。若不是，換到支援 `systemd` 的使用者 session 再安裝；若是，就用 `cortex service status` / `cortex service logs` 觀察目前 runtime 模式，不要假設所有機器都會以同一種方式運作。

## executor 未登入

live probe 最直接：

```bash
cortex doctor --probe-live --repo owner/name --json
```

若 bootstrap 或 doctor 指出 executor 未登入，先依你使用的工具完成登入，再重跑 bootstrap 或 request。這一類問題不能靠重啟 manager 解決，因為缺的是 executor 的外部身份狀態。

## stale venv 或 exec path drift (F34)

這是 `inspect service` 專門要抓的情境：service 還活著，但其實跑在已刪掉或過期的 `venv`。

```bash
cortex inspect service --instance cortex --json
```

若結果顯示執行中的 `venv`、exec path 或版本與目前安裝不一致：

1. 先不要反覆 `tick`。
2. 依 [Upgrade](upgrade.md) 或 [Rollback](rollback.md) 重裝。
3. 重啟 `cortex service restart --instance cortex`。
4. 重新執行 `cortex inspect service --json` 確認 drift 消失。

## 什麼時候該直接走 Runbook

當你已經知道是哪一類事故，而且需要留操作紀錄、做 contain / recover / verify，而不是單純查原因時，直接看 [Runbook](runbook.md)。
