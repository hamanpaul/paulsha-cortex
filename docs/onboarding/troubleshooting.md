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
| delivery preflight failed | `cortex inspect doctor --json` | `PSC_PREFLIGHT_CMD` 未設定、格式錯誤、shell wrapper 或 executable 不可執行 | 看 [delivery preflight 失敗](#delivery-preflight-失敗) |
| `systemd` 不可用 | `cortex service status --json` | 在沒有 `systemd --user` 的環境操作，或 unit 未安裝 | 看 [systemd 不可用](#systemd-不可用) |
| executor 未登入 | `cortex doctor --probe-live --repo owner/name --json` | `copilot`、`claude`、`codex` 尚未登入 | 看 [executor 未登入](#executor-未登入) |
| F34 / stale `venv` | `cortex inspect service --json` | unit 指向已刪的安裝位置，或 service 還在跑舊碼 | 看 [stale venv 或 exec path drift](#stale-venv-或-exec-path-drift-f34) |
| build 卡全數在採信階段被拒（`gate-ledger-missing-expected-gate`），但 gate 宣告看起來正常 | `sudo -u <gate 帳號> env HOME=<gate HOME> python3 -m pytest --version` | 降權部署下 gate 宣告的 `python3` 解析到**系統層** interpreter，而 `pytest` 只裝在 operator 的 user site-packages（`ProtectHome` 之後不可達） | 看 [降權部署下 gate ledger 為空](#降權部署下-gate-ledger-為空) |
| monitor 起得來但兩個 github provider `degraded`；doctor 的 `gh-*` probe 全紅 | `sudo -u <manager 帳號> env HOME=<manager HOME> gh auth status` | 降權部署下 Manager 沒有自己的 `gh` 登入態（operator 的在 `/home` 底下，`ProtectHome` 之後不可達） | 看 [降權部署下 Manager 沒有 gh 登入態](#降權部署下-manager-沒有-gh-登入態) |
| 升級後每一次派工都 `job-runner-path-undeclared` | `sudo grep PSC_ /opt/cortex/etc/cortex-manager.env` | #679 起三個 `PSC_*_PATH` 是**必填**；升級前的部署一個都沒宣告 | 看 [job-runner-path-undeclared](#job-runner-path-undeclared) |

## delivery preflight 失敗

`cortex inspect doctor --json` 若顯示 `preflight` 失敗，請先對照原因：

| 原因 | 對應訊息關鍵字 | 修復方式 |
| --- | --- | --- |
| 未設定 `PSC_PREFLIGHT_CMD` | `required` | 設定環境變數為 typed argv，確認 delivery preflight 可直接執行 |
| 命令格式不符 | `malformed` | 使用 argv 參數而非 shell 指令字串 |
| 使用 shell wrapper | `shell-wrapper-not-allowed` | 改用直接 module/executable，例如 `python3 -m paulsha_cortex.preflight_ci`（僅在 argv 第一段真的是 `bash`／`sh` 且帶 `-c` 時才會命中這一類） |
| executable 不可用 | `executable-unavailable` | 安裝/調整 PATH，或改用可執行的絕對/相對命令，避免 shell 包裹。**服務化部署**（`ProtectHome=yes`）下 `$HOME` 底下的路徑一律不可達，值請指向部署樹內的絕對路徑，例如 `/opt/cortex/venv/bin/python3 -m paulsha_cortex.preflight_ci` |

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

## 降權部署下 gate ledger 為空

**只發生在 Phase 2b 的降權部署（四分）**，症狀離原因很遠：dispatch 走得完、builder 交得
出合格 candidate，但每張帶 `test_policy` 的卡都在 harvest 撞
`gate-ledger-missing-expected-gate`，而唯一的痕跡是 `manager.log` 裡的一行。

成因：`PSC_GATE_CMD_PYTEST="python3 -m pytest -q"` 用的是**相對名**，由 gate 的
`PSC_GATE_PATH` 解析 ⇒ `/usr/bin/python3`（**系統層那一支**）。gate unit 自己的
`ExecStart` 用的是部署 venv 的 interpreter，但那只涵蓋 ledger writer 本身——**operator
宣告的命令另外解析一次**。`pytest` 只裝在 operator 的 `~/.local/...` 時，`ProtectHome=yes`
之後系統層的 `python3` import 不到它。

```bash
# HOME 由產生器導出，不要手寫絕對路徑（換 layout 時這裡會跟著動）
GATE_HOME="$(python3 -c 'from paulsha_cortex.trust_root.permgen import DEFAULT_LAYOUT as L; print(L.home_of("cortex-gate"))')"
# ⚠️ 這一條問的是「**系統層那支** python3 import 得到 pytest 嗎」，因此 interpreter 走
#    絕對路徑、且**不自帶 PATH**（#679）。「gate 實際會解到哪一支 python3」是另一個
#    命題，走 Runbook 第 4e-2 步的反向不變式——兩者混在一起正是 #679 的機制。
SYS_PY="$(command -v python3)"   # 系統層那一支（不是部署 venv 的）
sudo -u cortex-gate env HOME="$GATE_HOME" "$SYS_PY" -m pytest --version
#   `No module named pytest` ⇒ 就是這一條
```

`cortex doctor` 的 `gate-declarations` probe **驗不到這個**——它只驗宣告的 argv 形狀，不驗
「那個模組 import 得到嗎」。處置見 Runbook 第 4f 步（`sudo pip install
--break-system-packages 'pytest>=7' 'PyYAML>=6'`，並以 gate 身分＋完整加固面各實測一次）。
`PyYAML` 也要裝：gate 的 cwd 是被驗那棵樹的副本，`import paulsha_cortex` 會解到那棵樹，
而它 `import yaml`——缺它的症狀是 pytest exit code `2`（collection error）。

## 降權部署下 Manager 沒有 gh 登入態

同一族的另一個：Manager／monitor 的 system unit 帶 `ProtectHome=yes`，operator 的
`~/.config/gh/` 不可見。

```bash
MANAGER_HOME="$(python3 -c 'from paulsha_cortex.trust_root.permgen import DEFAULT_LAYOUT as L; print(L.home_of(L.manager_account))')"
sudo -u cortex-manager env HOME="$MANAGER_HOME" gh auth status
#   `You are not logged into any GitHub hosts.` ⇒ 就是這一條
```

處置見 Runbook 第 4g 步。**兩個檔要分別落位、owner 刻意不同**：`hosts.yml` 由服務帳號擁有
（`0600`，token 要 refresh 得回來），`config.yml` 維持 `root:root 0644`（它的 `aliases`
可宣告 `!` shell alias，讓服務帳號改得了它等於多一條執行面）。若落位後仍顯示未登入，先確認
沒有人設了 `XDG_CONFIG_HOME` 或 `GH_CONFIG_DIR`——那會讓 `gh` 去看別的目錄，而檔案還在原處。

## job-runner-path-undeclared

**升級到 #679 之後才會出現，而且是刻意的。** 症狀：降權模式下每一次派工都在 Manager
端當場失敗，理由 `job-runner-path-undeclared: PSC_BUILDER_PATH 未宣告…`。

成因：#679 之前 `build_job_env()` 對三個 `PSC_*_PATH` 是 **fail-open** 的——未宣告時
job 靜默拿到 **Manager daemon 的** `PATH`（那份值是否含 `<toolchain>/bin` 純看該機器的
EnvironmentFile 被誰手動加過什麼；Manager 自己也沒有 `PATH` 時，`os.execvpe` 退回
`os.defpath`＝`:/bin:/usr/bin`）。兩種情形下 `codex` 都可能**靜默**解到 `/usr/bin/codex`
（實機 0.42.0，toolchain 那份是 0.147.0）：不報錯，只是每一筆產出都來自一支從未被判讀
過的 CLI。現在未宣告即當場失敗——**「當場失敗且訊息可讀」嚴格優於「靜默跑錯版本」**。

```bash
# 值由產生器導出，不要手打（三個角色各一份）
for flag in --job --review-job --gate-job; do
  python3 -m paulsha_cortex.trust_root unit four-way "$flag" | grep '^Environment=PATH='
done
```

完整的升級步驟（補三個變數、重新落檔六份模板 unit、重啟 Manager、跑反向不變式）在
Runbook 第 **5-5b** 步。**不要**把 `PSC_JOB_RUNNER` 改回 `direct` 來繞開：那會讓 Phase 2b
的全部隔離一次失效，只為了避開一個補三行設定就能解的錯誤。

## 什麼時候該直接走 Runbook

當你已經知道是哪一類事故，而且需要留操作紀錄、做 contain / recover / verify，而不是單純查原因時，直接看 [Runbook](runbook.md)。
