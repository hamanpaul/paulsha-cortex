# Upgrade

這份文件處理日常升級，以及 dogfood F1 類型的 pipx 快照過期問題：systemd unit 或 shell 仍指向舊的安裝位置，導致你以為升級成功，實際上 manager 還跑在舊碼或失效 venv。

## 引用來源

- `docs/superpowers/specs/onboarding-docs-spec.md`
- `docs/superpowers/specs/porcelain-bootstrap-design.md`
- `docs/superpowers/specs/porcelain-service-spec.md`
- `docs/superpowers/specs/porcelain-inspect-spec.md`
- issue #94
- dogfood findings: F1、F34
- `python3 -m paulsha_cortex.cli service status --help`
- `python3 -m paulsha_cortex.cli service restart --help`
- `python3 -m paulsha_cortex.cli inspect service --help`

## 何時該升級

- 你要跟上新的 CLI 家族或 workflow 契約
- `cortex --version` 與預期版本不一致
- `cortex inspect service --json` 顯示執行中的 `venv` 或 exec path 漂移
- 你曾刪過舊 worktree、舊 pipx snapshot，懷疑 service 還指向不存在的位置

## 標準升級流程

1. 先記錄目前狀態。

```bash
cortex --version
cortex service status --instance cortex --json
cortex inspect service --instance cortex --json
```

2. 重新安裝最新版本。

```bash
pipx install --force git+https://github.com/hamanpaul/paulsha-cortex.git
```

3. 重啟 manager service/timer，讓 systemd 重新載入目前安裝位置。

```bash
cortex service restart --instance cortex
```

4. 驗證目前 runtime 已指向新版本。

```bash
cortex --version
cortex service status --instance cortex --json
cortex inspect service --instance cortex --json
```

5. 若這是 production repo，再補一次 live probe。

```bash
cortex doctor --probe-live --repo owner/name --json
```

## F1：pipx 快照過期時怎麼看

常見徵兆：

- `cortex --version` 已更新，但 manager 行為仍像舊版
- `cortex inspect service --json` 顯示的 exec path 或 `venv` 指向不存在的位置
- `systemctl --user status` 顯示 unit 存活，但 CLI 看起來像沒套到最新碼

遇到這種情況，不要只重跑單一命令，直接走上面的完整升級流程。F1 的重點不是「重新開一次 shell」，而是讓 pipx 與 service runtime 一起回到同一個有效安裝位置。

## 非 pipx 安裝的補充

若你是在 repo checkout 內用 `python -m pip install .` 安裝，等價操作是：

```bash
python -m pip install --upgrade .
cortex service restart --instance cortex
```

之後同樣要用 `cortex inspect service --json` 驗證 runtime 沒有殘留舊 `venv`。
