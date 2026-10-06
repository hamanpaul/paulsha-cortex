---
status: accepted
work_item: dangerous-command-blocklist
---

# 以高危指令阻擋清單取代人工在場的安全邊界：政策引擎與 builder executor 阻擋

## Boundary

- Issue：`hamanpaul/paulsha-cortex#1283`，含 issue 內 owner 2026-10-06 的裁決留言。
- owner 原則：cortex 是高可信自主開發工具，不要停下來等人的機制；安全邊界改成擋特定高危指令
  （例如 `rm -rf /`、`passwd`、`adduser`）。
- owner 裁決：
  1. 使用者層級（direct mode）的 model job **可以 sudo**，只擋特定危險指令；**不採用**
     `NoNewPrivileges` 全面禁止 sudo。已知限制（model 先寫腳本再執行可繞過）已說明並獲接受。
  2. 日常工作逐步移到 Trust Root system 部署；不為使用者層級另做 Landlock。
- 本票是第一刀：政策引擎，加上目前三個 builder executor（codex、copilot、agy）與 claude reviewer 的阻擋。
  builder 不是 claude，所以只做 Claude hook 擋不到 builder。Manager 對外 git／gh 白名單、GitHub ruleset
  檢查、既有人工等待點的收斂，都留給後續票。
- 命中處置不得等人：拒絕該次 tool call、job 繼續；`critical` 命中或同一 job 累計達上限才結束 job；同一 run
  第一次 `policy-violation` 自動換 model identity 重派一次；第二次自動結束 run 並在 issue 留言。全程不得出現
  `needs_human` facet。
- 不放寬任何既有的沙箱或 fail-closed 行為。

## Tasks

- [ ] **T1 實測各 executor 的阻擋能力（先做，記錄在 PR 內）**：在本機量測目前版本的 codex（hooks.json
      PreToolUse 或 execpolicy `.rules` 的 `forbidden`）、copilot（`--deny-tool 'shell(<cmd>)'` 是否優先於
      `--allow-all-tools`、`preToolUse` hook）、agy（settings deny 或 hook）、claude（PreToolUse 的 Bash
      matcher 與 `permissions.deny`）實際能否擋下 shell 指令。量不到能力的 executor 記為
      `unsupported-measured` 並附量測紀錄，不得靜默缺格。
- [ ] **T2 政策引擎與基準政策**：新增純函式、無外部依賴的 `paulsha_cortex/command_policy.py`，以解析後的
      argv 比對，不做字串 grep。涵蓋：切段、argv0 正規化、wrapper 剝殼（`sudo`、`env`、`timeout` 等）、
      `sh -c`／`bash -c` 遞迴、`python -c` 字面引數、路徑正規化與保護根、git／gh 子命令選項。基準政策
      `coordinator/data/command-policy.yaml`（versioned）；operator overlay 只能加嚴，不認得的鍵或想降級時
      fail closed，並自動重驗。
- [x] **T3 corpus 測試**：`tests/fixtures/command-policy/corpus.yaml`，每條規則至少一個應擋、一個應放。
      規避案例至少包含：`\rm -rf /`、`/bin/rm -rf /`、`rm -r -f /*`、`sudo -E rm -fr ~`、`env -S 'rm -rf /'`、
      `bash -c "rm -rf /"`、`sh -c 'sudo passwd root'`、`sudo useradd x`、`sudo adduser x`、
      `echo 'x ALL=(ALL) NOPASSWD: ALL' | sudo tee /etc/sudoers.d/x`、`find / -delete`、
      `python3 -c "import shutil; shutil.rmtree('/')"`、`dd if=/dev/zero of=/dev/sda`、
      `git push -f origin main`、`git push origin :main`、`gh repo delete o/r --yes`。誤判對照至少包含：
      `rm -rf build/ node_modules`、`echo "rm -rf /"`、`grep passwd /etc/group`、`sudo apt-get install -y jq`、
      `dd if=a.img of=b.img`、`python -m pytest`。import 時斷言每條規則都有雙向案例。
- [ ] **T4 executor 阻擋接線**：依 T1 結果，把政策編譯並注入 codex、copilot、agy builder 與 claude reviewer
      的命令阻擋機制（hook 或 deny 規則，經 launcher 的 argv／受管設定注入，job 無法透過改 argv 繞過）。
      新增 `EXECUTOR_POLICY_ENFORCEMENT` 覆蓋表：每個 executor × role 必須明示 `hook-enforced`／
      `deny-rules-only`／`unsupported-measured`／`not-applicable`，import 時斷言涵蓋全部 argv builder。
- [ ] **T5 命中處置與稽核**：新增 `policy-violation` outcome（不是 transient、不燒 provider 重試額度），實作
      Boundary 所述的自動處置。每次決策記錄事件（rule id、類別、severity、遮蔽後的 argv、policy sha256），
      `cortex status`／`work show` 顯示違規計數與最近一筆。
- [ ] **T6 文件**：`docs/command-policy.md` 說明政策格式、overlay 規則、各 executor 的覆蓋程度與已知限制。
      新增 changelog fragment，並同步 `CHANGELOG.md [Unreleased]`。
