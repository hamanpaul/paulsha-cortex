---
status: accepted
work_item: upgrade-venv-umask
---

# cortex upgrade 的 umask 077 讓新 venv 不可執行，升級 verify 失敗

## Boundary

- Issue：`hamanpaul/paulsha-cortex#1295`。
- 現場：在 adoption 主機上執行 `cortex upgrade 0.1.14`。apply 與 activate 都通過，但三個服務都
  `Failed to execute /opt/cortex/venv/bin/cortex: Permission denied`（203/EXEC），verify 失敗並自動回滾。
  新 venv `/opt/cortex/venvs/<wheel sha>/bin` 的權限是 `0700 root`。原因是 `upgrade.py` 執行
  `os.umask(0o077)`，candidate 的 apply 子程序繼承了它。
- RC 的升級演練帶 `--allow-same-version`，重用了已存在的同 sha venv，所以從沒有在 umask 077 下新建過
  venv。回滾後權限錯誤的 venv slot 仍留在主機上，下一次升到同一個 wheel 時會被重用。
- 修正必須在 candidate 端（installer apply）生效：正式主機現行的 orchestrator 是 0.1.13，升到含此修正的
  版本時，由新版 candidate 執行 apply。
- 不放寬任何既有的權限或 fail-closed 檢查；機敏檔案仍維持 0600。

## Tasks

- [x] **T1 RED**：以 umask 0077 建立 venv slot，斷言服務帳號無法執行 `bin/cortex`；以權限錯誤的既有同 sha
      slot 執行重用路徑，斷言現行不會修正。
- [x] **T2 venv 權限明確化**：installer 建立或重用 venv slot 時，明確設定 root 擁有、目錄與執行檔 0755、
      其他檔案 0644（或與既有 slot 相同的規則），不依賴呼叫端 umask。重用既有 slot 時驗證並修正，修正動作
      要能被 rollback 正確處理。
- [x] **T3 orchestrator umask 規範**：`cortex upgrade` 對 candidate 子程序的 umask 有明確規定；機敏檔案由寫入端
      自己設 0600，不靠全域 umask 讓 installer 產物不可讀。
- [x] **T4 verify 診斷**：服務停在 `activating` 或 203/EXEC 時，verify 的失敗訊息帶出 journal 的原因。
- [x] **T5 RC**：升級演練涵蓋「新 wheel sha、新建 venv」，並在呼叫端 umask 0077 下執行，驗證服務帳號能執行
      新 venv、verify 通過。
- [x] **T6 文件**：runbook 補上說明。新增 changelog fragment，並同步 `CHANGELOG.md [Unreleased]`。
