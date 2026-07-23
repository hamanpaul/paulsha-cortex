# Rollback

這份文件處理升級後需要退回上一個已知可用版本的情況，尤其是 F1 類型的 pipx / service 指向漂移。回滾的原則是：CLI 版本、service runtime、實際 `venv` 三者要一起回到同一個版本。

## 引用來源

- `docs/superpowers/specs/onboarding-docs-spec.md`
- `docs/superpowers/specs/porcelain-service-spec.md`
- `docs/superpowers/specs/porcelain-inspect-spec.md`
- issue #94
- dogfood findings: F1、F34
- `python3 -m paulsha_cortex.cli service restart --help`
- `python3 -m paulsha_cortex.cli inspect service --help`

## 什麼時候要回滾

- 升級後出現新的阻斷性錯誤
- manager 啟動後馬上失敗
- `cortex inspect service --json` 顯示新的安裝位置失效
- 你已經知道某個 tag 或 commit 是最後一個可用版本

## pipx 回滾流程

1. 決定要回滾到哪個 tag 或 commit。

2. 重新安裝該版本。

```bash
pipx install --force "git+https://github.com/hamanpaul/paulsha-cortex.git@<known-good-ref>"
```

3. 重啟 service。

```bash
cortex service restart --instance cortex
```

4. 驗證回滾已生效。

```bash
cortex --version
cortex service status --instance cortex --json
cortex inspect service --instance cortex --json
```

## 驗證重點

- `cortex --version` 已回到預期版本
- `cortex service status --json` 能正常回報 mode 與 unit 狀態
- `cortex inspect service --json` 不再報 stale exec path、失效 `venv` 或 F34 類型 drift

## 回滾後的建議

- 先保留造成問題的版本號、time window、錯誤訊息
- 若是 request timeout、manager degraded 之類症狀，回滾後再跑一次對應 SOP，見 [Runbook](runbook.md)
- 下一次再升級前，先用 [Upgrade](upgrade.md) 的流程做完整驗證，不要只看 `cortex --version`
