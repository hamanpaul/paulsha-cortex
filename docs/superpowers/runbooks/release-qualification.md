# Release qualification and deployment canary

這兩條 workflow 回答不同問題，不可互相替代：

| Workflow | 回答的問題 | Credentials | 外部寫入 | 阻擋 GitHub Release |
| --- | --- | --- | --- | --- |
| `RC qualification` | exact wheel 能否安全安裝、啟動、驗證、回滾，且權限不變量是否成立？ | 不需要；只走固定非秘密 fixture | 禁止；container runtime 使用 `--network none` | 是，必須為同一 main SHA |
| `Deployment canary` | 此刻的 provider 登入／配額／model、Manager GitHub 與完整派工是否健康？ | 需要 protected environment | 僅限明示 probe repository | 否 |

## 發版操作

1. 合入 release candidate PR，確認 default branch exact SHA 的 `Tests` workflow 全綠。
2. 在 Actions 手動執行 `RC qualification`，ref 必須選同一個 default branch SHA。
3. workflow 會產生 `rc-qualification-<sha>`；`qualification.json` 必須為
   `schema_version: 2`、`profile: release`、`providers: []`。
4. 手動執行 `Release`，輸入與 `VERSION` 完全相同的版本。release workflow 會重新 build wheel、
   比對 wheel/bundle hashes，並以 `--require-release-profile` 驗證 evidence。
5. 驗證 annotated tag、non-draft GitHub Release 與唯一 wheel asset 都指向同一 main SHA。

這條路徑不需要設定 GitHub environment secrets 或 variables；若 `RC qualification` 要求它們，
代表 workflow contract 已退化，應先修復而不是補值。

兩條 qualification workflow 的 checkout 都必須使用 `fetch-depth: 0`，並在建立 source bundle
前確認 `git rev-parse --is-shallow-repository` 為 `false`。depth-1 bundle 會漏掉 candidate 的
parent history，直到 installer 執行 repository `fsck` 才以 drift 失敗，不能作為 release evidence。

## 可選的 live canary

只有要驗特定部署環境時才執行 `Deployment canary`。它使用 protected
`rc-qualification` environment，既有 live inputs 為：

- secrets：`CORTEX_RC_CODEX_AUTH`、`CORTEX_RC_AGY_AUTH`、
  `CORTEX_RC_COPILOT_AUTH`、`CORTEX_RC_MANAGER_GITHUB_AUTH`
- variables：`CORTEX_RC_PROBE_REPOSITORY`、`CORTEX_RC_PROBE_WORK_ID`、
  `CORTEX_RC_PROBE_ISSUE`

canary 會產生 `deployment-canary-<sha>`，其 evidence profile 必須是
`deployment-canary`。provider login/quota/model mismatch、fallback、Manager dry-run ref drift 或
full dispatch 未 terminal 都會讓 canary 失敗，但 release workflow 永遠不查詢該 artifact。
