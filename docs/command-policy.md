# 高危指令阻擋政策

此功能在 executor 呼叫 shell 工具前，依解析後的 argv 阻擋基準政策列出的高危命令。政策 hook 只限 cortex 派出的 job；沒有 `PSC_JOB_ID` 時完全 no-op。它補上命令層防線，不取代既有 sandbox、權限隔離或 fail-closed 檢查。

已接受的邊界是：使用者層級 job 可以使用 `sudo`，只阻擋特定危險命令；不以 `NoNewPrivileges` 全面封鎖 sudo，也不新增使用者層級 Landlock。政策不能完整檢查模型先建立腳本、再用允許命令執行的內容。

## 基準政策

版本化基準位於 [`coordinator/data/command-policy.yaml`](../coordinator/data/command-policy.yaml)。相同內容會打包到 `paulsha_cortex.coordinator` 的 package data，供安裝後的 hook 載入；測試會檢查兩份內容一致。

基準採 YAML schema version 1，包含：

| 欄位 | 用途 |
| --- | --- |
| `wrappers` | 可剝除後繼續檢查的命令 wrapper，例如 `sudo`、`env`、`timeout` |
| `protected_paths` | `rm`／`find`／`python` 的受保護路徑 |
| `protected_sudoers_paths` | 阻擋寫入 sudoers 檔案的路徑 |
| `block_device_prefixes` | `dd` 不得覆寫的裝置路徑前綴 |
| `protected_refs` | `git`／`gh` 不得強制改寫的分支 |
| `rules` | 已實作的規則 id、命令、類別、嚴重度與摘要 |

引擎先切分 shell 命令段，再比對 argv；會剝除開頭的 `VAR=value` 指派、正規化 argv0、剝除支援的 wrapper（含 `xargs`、`eval`），遞迴處理 `sh -c`／`bash -c`、括號 subshell、`$()` 與反引號命令替換，並檢查 `python -c` 的字面引數、路徑、sudoers 寫入、block device 目的地及 git／gh 子命令選項。`$HOME` 與 `${HOME}` 會正規化為受保護的 `~` 路徑。未知 rule、wrapper、policy key 或格式錯誤一律拒絕載入。

## Operator overlay

`PSC_COMMAND_POLICY_OVERLAY` 指向可選 YAML 檔。Overlay 只能增加保護目標：

```yaml
add_protected_paths:
  - /srv/production-data
add_protected_sudoers_paths:
  - /etc/sudoers.d/site-policy
add_block_device_prefixes:
  - /dev/mapper/critical
add_protected_refs:
  - release
```

Overlay 不接受重寫 rules、wrappers、severity 或既有欄位；未知欄位、非字串清單或不能通過完整基準驗證的值都會 fail closed。每次 hook 執行都重新載入並驗證有效政策，稽核事件記錄有效政策的 SHA-256。

## Executor 接線與量測

| Executor / role | Enforcement | 注入方式與本機量測 |
| --- | --- | --- |
| Codex builder | `unsupported-measured` | 本機 `codex-cli 0.159.3` 的 `codex exec --help` 顯示 `--enable` 與 `-c`；launcher 以 argv 注入 `PreToolUse` hook。 |
| Copilot builder | `unsupported-measured` | 本機 `GitHub Copilot CLI 1.0.92` 的 `copilot --help` 顯示 `--deny-tool`、`--allow-all-tools` 與 `--allow-all`；launcher 為 job 寫入唯一 `COPILOT_HOME/hooks` 設定。原生 deny rules 作靜態補充，path-sensitive／wrapper 規則由 hook 判定。 |
| Antigravity builder | `unsupported-measured` | 本機 `agy 1.3.0` 由 launcher 在該 job 的 Antigravity CLI home 安裝唯一 plugin，plugin 內含 `hooks.json` 的 `PreToolUse` hook；job 結束後移除該 plugin。 |
| Claude builder | `unsupported-measured` | 本機 `Claude Code 2.1.291`；launcher 經每次執行的 `--settings` 注入 `PreToolUse` Bash matcher。 |
| Claude reviewer | `unsupported-measured` | launcher 經每次 review 的 `--settings` 注入相同的 `PreToolUse` Bash matcher。 |
| 其他 argv builders / roles | `not-applicable` | `EXECUTOR_POLICY_ENFORCEMENT` 對每個 argv builder × planner／builder／reviewer 明列狀態，module import 時驗證完整覆蓋。 |

`tests/test_command_policy_hook_1283.py` 以各 executor 的原生 payload 形狀執行 hook，確認拒絕輸出、稽核事件與 critical job termination；launcher 測試檢查注入的 argv、settings、plugin 與 hook 設定。量測沒有啟動模型 API session，也沒有讓 executor 真正送出 shell call，因此 matrix 對四種 executor 的必要角色都標為 `unsupported-measured`。此狀態表示目前只有 CLI／設定注入與 hook 自身測試證據；必須完成 executor 到 hook 的端到端阻擋量測後，才能改標 `hook-enforced`。Copilot 的 command hook 超時依其文件屬 fail-open；此處設 5 秒 timeout，並以原生 deny rules 覆蓋少數靜態命令。其餘動態命令依賴 hook 正常載入。

命令解析會檢查括號 subshell、`$()`、反引號，以及經 `xargs`／`eval` 執行的命令。它不會執行 shell 展開，也無法分析模型先建立或改寫腳本、再用允許命令執行的內容；這是已接受的邊界。

原生介面依據：

- [Codex hooks](https://developers.openai.com/codex/hooks/)
- [GitHub Copilot CLI hooks reference](https://docs.github.com/en/copilot/reference/hooks-reference) 與 [tool allow／deny precedence](https://docs.github.com/en/copilot/how-tos/copilot-cli/use-copilot-cli/allowing-tools)
- [Antigravity hooks](https://www.antigravity.google/docs/hooks/) 與 [plugin format](https://www.antigravity.google/docs/cli/plugins)
- [Claude Code hooks](https://code.claude.com/docs/en/hooks)

## 違規處置與稽核

Hook 拒絕該次 tool call，並把事件 append 到 job log 旁的 `.command-policy.jsonl`。事件包含 rule id、category、severity、遮蔽後 argv、executor、job id、timestamp 與有效政策 SHA-256。事件檔不是一般 provider log；缺損、非 regular file、symlink 或格式不符會形成 critical integrity event。

- `critical` 命中即終止該 job 的 process group；`high` 命中先拒絕該次呼叫，同一 job 第三次命中才終止 job。
- Manager 將 job outcome 記為 `policy_violation`，不當成 transient，不建立 provider backoff。
- 同一 run 第一次收到此 outcome 時，Manager 依現有候選、backoff 與 runtime preflight 選擇另一個 model identity 重派一次。
- replacement job 再次觸發政策，或沒有可用替代 identity 時，Manager 將 run 自動標記 failed 並在關聯 issue 留下稽核摘要；這條處置不新增 `needs_human` facet。
- `cortex status` 與 `cortex work show` 的 JSON read model 顯示違規總數及最近事件。

## 驗證

聚焦回歸測試：

```bash
python -m pytest -q tests/test_command_policy_1283.py tests/test_command_policy_hook_1283.py
```

完整 pytest 與交付 preflight 仍由 workflow card gate 執行。
