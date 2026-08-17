# Quickstart

目標：第一次接觸 `paulsha-cortex` 的使用者，能在 10 分鐘內完成安裝、bootstrap，並看到第一個 workflow 被建立與送進 manager 流程。

## 引用來源

- `docs/superpowers/specs/2026-07-21-porcelain-cli-ux-design.md` §6、§9
- `docs/superpowers/specs/onboarding-docs-spec.md`
- `openspec/specs/onboarding-documentation/spec.md`
- issue #94
- dogfood findings: F1、F8
- `python3 -m paulsha_cortex.cli --help`
- `python3 -m paulsha_cortex.cli bootstrap --help`
- `python3 -m paulsha_cortex.cli init-sample --help`
- `python3 -m paulsha_cortex.cli ready --help`
- `python3 -m paulsha_cortex.cli run tick --help`

## 0. 先備條件

- Python 3.10+
- Git
- 至少一個已安裝且已登入的 executor CLI：`copilot`、`claude` 或 `codex`
- 你要治理的 git repo

Quickstart 只用既有 CLI，不要求你先理解 deck/spec 的內部模型；若要理解名詞，再回頭看 [Concepts](concepts.md)。

## 1. 安裝

推薦：部署**特定 release**（例如目前最新 `v0.1.4`），從 tag 或 GitHub Release
附帶的 wheel 安裝，取得已在 Python 3.10–3.13 完整 CI 驗證過、可回滾的版本：

```bash
# 從 release tag
pipx install "git+https://github.com/hamanpaul/paulsha-cortex.git@v0.1.4"

# 或從 GitHub Release 下載的 wheel（release.yml 會把 wheel/sdist 附在該 tag 的 Release）
pipx install ./paulsha_cortex-0.1.4-py3-none-any.whl

cortex --version   # 應印出 0.1.4
cortex --help
```

若要追 `main` 最新碼（**development / edge channel**，會拿到尚未發版、未經
release smoke 驗證的變更）：

```bash
pipx install git+https://github.com/hamanpaul/paulsha-cortex.git
```

若你是在 repo checkout 內直接試跑，也可以用：

```bash
python -m pip install .
python3 -m paulsha_cortex.cli --version
```

## 2. 設定 delivery preflight 命令

在第一次執行 `cortex bootstrap` 前，請先設定 `PSC_PREFLIGHT_CMD`，避免第一次就撞到 delivery preflight 的 fail-closed。

```bash
export PSC_PREFLIGHT_CMD='python3 -m paulsha_cortex.preflight_ci'
```

cortex 內建的 `paulsha_cortex.preflight_ci` 是一個 typed-argv 轉接器，會把 delivery 的
`--pr <N>` / `--metadata <路徑>` / `--skip-tests` 翻譯給 `paulsha-conventions` 的
CI-parity preflight（`policy_check.preflight`）。使用它需要在**同一個 Python 環境**裝上與
`.project-policy.yml` 的 `policy_version` 逐字相同的 `policy-check`；版本對不上時引擎會
fail-closed 並直接說出期望版本。

若你的 repo 有自己的 preflight，把值換成該 module / executable 即可（同樣要是 typed argv）。
請避免使用 shell wrapper（例如 `sh -c`）；也不要指向 `$HOME` 底下的路徑——服務化部署帶
`ProtectHome=yes` 時那些路徑一律不可達。

在 `bootstrap` 或 `cortex inspect doctor --json` 前後都可用下面指令驗證：

```bash
cortex bootstrap --repo-root "$(git rev-parse --show-toplevel)"
cortex inspect doctor --json
```

## 3. 跑一次 bootstrap

先進到你要治理的 repo：

```bash
cd ~/path/to/target-repo
```

先做 dry-run，確認 Python、Git、repo root 與 executor 登入態：

```bash
cortex bootstrap --dry-run --repo-root "$(git rev-parse --show-toplevel)"
```

確認沒問題後正式執行：

```bash
cortex bootstrap --repo-root "$(git rev-parse --show-toplevel)"
```

如果你只想先安裝 unit、不立刻啟動 service，可以改用：

```bash
cortex bootstrap --repo-root "$(git rev-parse --show-toplevel)" --no-start
```

bootstrap 完成後，先看目前 runtime 狀態：

```bash
cortex service status --instance cortex --json
cortex inspect doctor --json
```

## 4. 建立第一個 workflow

建立一個 sample workflow：

```bash
cortex init-sample --task "demo feature" --change demo-feature
```

這一步會產生一份 `dispatch: hold` 的 sample spec，並列出你還要補的欄位。照輸出把 sample spec 補齊後，再做兩件事：

1. 把 `plan`、`target_branch`、`verification` 改成你的真實值。
2. 把 `dispatch: hold` 改成 `dispatch: auto`。

如果你只是想看 manager 能否接住第一個 workflow，最小可行流程是：

```bash
cortex ready --specs-dir "$HOME/.agents/specs"
cortex run tick --wait
```

`cortex ready --specs-dir "$HOME/.agents/specs"` 會告訴你 sample spec 是否已經符合派工條件；`cortex run tick --wait` 會把 ready 的 workflow 送進 manager，並等待 request 進入 terminal 或 timeout。

## 5. 觀察第一個 workflow 的結果

```bash
cortex request list
cortex jobs
cortex status
```

- `cortex request list`：看最近一次 `run tick` request 是否還在處理中。
- `cortex jobs`：看 builder/reviewer job 是否被建立。
- `cortex status`：看整體 manager gate 與 slice 狀態。

若 `--wait` 超時，先不要重跑 mutation。改看：

```bash
cortex request logs <request-id>
cortex request wait <request-id> --timeout 30
```

request timeout 是 dogfood F8 的典型情境：CLI 等待超時不代表 manager 沒在工作，只代表前端等待視窗結束了。

## 5. 下一步

- 想升級已安裝版本：看 [Upgrade](upgrade.md)
- 想回退到上一個已知可用版本：看 [Rollback](rollback.md)
- 想查名詞與生命週期：看 [Concepts](concepts.md)
- 想處理故障：看 [Troubleshooting](troubleshooting.md) 與 [Runbook](runbook.md)
