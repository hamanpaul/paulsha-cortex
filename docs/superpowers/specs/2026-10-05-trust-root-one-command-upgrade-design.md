---
status: proposed
work_item: trust-root-one-command-upgrade
issue: 1263
refs:
  - docs/superpowers/runbooks/trust-root-transactional-install.md
  - docs/superpowers/runbooks/trust-root-legacy-adoption.md
  - paulsha_cortex/trust_root/install/cli.py
  - paulsha_cortex/trust_root/install/core.py
---

# Trust Root 一鍵升級：`cortex upgrade <版本>`

## 1. 背景與目標

Trust Root system 部署目前每次升級，operator 都要照
`trust-root-transactional-install.md` 以 root 逐步下指令，包括：

- §1：release ingress、核對 asset digest、封存 candidate CLI；
- §2：plan 三方確認；
- §3：lease、停服務、apply；
- §4：credentials；
- §5：activate、verify，失敗時 rollback；
- §6：hard-crash recovery。

這些步驟在 installer 裡都已經是現成的子命令，但只能靠人手串起來。結果每升一版，owner 就要手動操作一整段，這不合理（#1263）。

**目標**：owner 要求升級時，只要下一個 root 指令。工具依序執行上述每一步，遇到任何失敗都會自動回到前一版，最後輸出結果摘要。

```bash
sudo /opt/cortex/venv/bin/cortex upgrade 0.1.13
```

### owner 裁決（2026-10-05）

- 只在 owner 要求時才升級。
- plan sha 由工具自己綁定，不需要人工三方確認。
- 沿用現有保護：
  - 只裝正式 release；
  - 核對 GitHub release asset digest 與 qualification manifest；
  - receipt、lease、fail-closed。
- 隨 0.1.13 發布。#1122 的 legacy adoption 改用 0.1.13 進行；之後每次升級都用這個指令。

### 非目標

- 自動偵測新版、背景常駐 updater、timer。
- 額外的簽章（sigstore）、緩衝期、plan 人工審查。
- 首次安裝與 legacy adoption：它們需要審核 inventory 與首次 credentials，維持 runbook 流程。
- 讓 cortex 元件（Manager、job、模型）觸發升級。`cortex upgrade` 只由 operator 以 root 執行。

## 2. 前提與適用範圍

- 主機已有一份由 installer 寫入、狀態為 applied／activated 的 **current receipt**（`--prior-receipt` 的來源）。沒有 receipt 的舊主機要先完成 legacy adoption（#1122）。
- current receipt 的定位：以 installer 已記錄的生效中 receipt 為準（receipt 目錄中狀態為 applied／activated 且未被取代的那一份），並與 `cortex service status --system` 回報的 loaded runtime 交叉核對。無法唯一判定時直接停止，不以檔名、時間戳或目錄掃描來猜。
- 只能升級；目標版本必須嚴格高於 current receipt 的版本。降版請用 installer 的 `rollback`，或依 runbook 手動處理，不在本指令範圍。
- 主機 overlay 沿用 current receipt 綁定的同一份 host overlay（`/var/lib/cortex-installer/host-overlay.yaml`），升級不更改 overlay。

## 3. 指令介面

```
cortex upgrade <version> [--wait-idle <秒>] [--repository <owner/repo>] [--json]
cortex upgrade --recover
cortex upgrade --status
```

- `<version>`：SemVer，例如 `0.1.13`，對應 tag `v<version>`。
- `--repository`：預設為 installer 設定中的 release 來源，正式部署固定為 `hamanpaul/paulsha-cortex`。這個參數只開放給 RC qualification 測試，用來指向本機假的 release 來源（見 §8）。
- `--wait-idle`：還有在飛 job 時，最多等待的秒數。預設為 0，也就是有在飛 job 就直接拒絕並說明。
- `--recover`：前一次升級在中途被 SIGKILL、OOM 或斷電打斷時使用，對應 runbook §6。
- `--status`：唯讀，顯示 current receipt 的版本、上一次升級的結果，以及 loaded↔installed 是否一致。
- 也提供 `cortex install trust-root upgrade` 這個完整路徑名稱，和 installer 其他子命令並列；`cortex upgrade` 是它的別名。

執行者必須是 root。不是 root 就立刻失敗，不做任何網路或檔案操作。

## 4. 流程

每一步都直接使用 installer 既有的函式或子命令，**不另寫第二套邏輯**。

1. **前置檢查**（不改任何東西）
   - 讀 current receipt：取得版本、plan，以及 host overlay 的 digest。
   - 確認目標版本嚴格高於 current receipt 的版本。
   - 檢查 host-global transaction lock 與 maintenance lease 都沒有被占用。
   - 檢查在飛 job：重用 legacy adoption 的 in-flight 判定（job 帳號的 process 數與 durable in-flight job 數）。有在飛 job 時，依 `--wait-idle` 等待或直接拒絕。
2. **release ingress**（runbook §1）
   - 透過 GitHub REST（HTTPS，公開 repo、不需 token）取得 annotated tag 的 commit target，以及三個 asset 的 `digest`。expected 值**一律來自 REST metadata**，不由下載後的檔案自己產生。
   - 下載 asset 到 `/var/lib/cortex-installer/<version>/release`。目錄與每一層 ancestor 都必須 root-owned、不可由 group／other 寫入、不可有 symlink（沿用 §1 的檢查）。
   - 逐一核對 asset digest。驗過 qualification manifest 後才導出 bundle hash、檢查 archive topology、解壓，再驗 bundle 列出的每一個檔案。
   - 用 wheelhouse 離線建立封存的 candidate venv（`--copies`）。
3. **plan**（runbook §2）
   - 以封存的 candidate CLI，在非 root、空白環境中產生 plan，並帶入 `--prior-receipt <current receipt>` 與同一份 host overlay。
   - 工具比對兩個值：CLI 回報的 plan sha，以及 plan 檔實際的 sha256。兩者不一致就停止。
   - plan 以 sha 命名，寫入 `/var/lib/cortex-installer/plans/`，作為後續 recovery 的 durable plan。
4. **lease 與停服務**（runbook §3）
   - 取得 maintenance lease。
   - 記下三個 service 原本是否 active，連同 receipt path 寫進 plan-bound maintenance snapshot，再停止三個 service。
5. **apply**：`apply --plan … --confirm-sha256 <工具綁定的 sha> --receipt <新 receipt> --prior-receipt <current>`。
6. **credentials**（見 §5）：沿用 current receipt 已安裝、已記錄的憑證，不需要 operator 重新指定來源。
7. **activate → verify**（runbook §5）
   - `verify` 必須 PASS，三個 unit 都必須 active。
   - `cortex service status --system` 必須顯示 loaded runtime 與新 receipt 的 installed artifact 一致（#841）。
8. **完成**
   - 釋放 lease、清除 snapshot。
   - 寫出 upgrade report：`/var/lib/cortex-installer/<version>/upgrade-report.json`。內容包含版本、各 asset digest、plan sha、新舊 receipt path、verify evidence path、各步驟耗時與結果。
   - 在終端機印出摘要。

## 5. 憑證

- activation 要求**新 receipt** 記錄 plan 的 `required_credentials`（`core.activate_receipt`）。目前的 RC 升級演練是在 apply 之後直接 rollback，**沒有涵蓋「升級後 activate」**，因此現行 installer 是否會把 prior receipt 的 credential 記錄帶進新 receipt，尚未被驗證過。
- 本設計要求：
  - 新 receipt 只能接手 prior receipt **已記錄**的 credential，條件是 `(principal, provider)` 相同、落點路徑相同，而且落點檔目前的 digest 與 prior receipt 的記錄一致。
  - 不從任何 HOME 探索憑證，也不讀憑證的值。
  - 新 plan 要求的 credential 如果不在 prior receipt 裡，例如新增了 provider，就停在 apply 之後、activate 之前，自動 rollback，並提示 operator 依 runbook §4 匯入。
- 如果現行 installer 沒有這個 handoff，就在 `core` 補一個 prior-receipt credential handoff（僅限上述條件），而不是在 upgrade 指令裡另外處理。

## 6. 失敗處理與 rollback

- **步驟 1–3 失敗**：主機完全沒有被改動。直接回報原因，exit code 非 0。
- **步驟 4 之後失敗**，包括 apply、credential handoff、activate、verify 失敗，以及 INT／TERM：
  - 以新 receipt 執行 installer `rollback`。
  - 只有 `restore_safe=true` 時，才依 snapshot 把原本 active 的 service 重新啟動。
  - 確認 current receipt 的 loaded↔installed 仍然一致。
  - report 記錄失敗步驟與 rollback 結果。
- **rollback 回報未知 drift**（`restore_safe=false`）：不重啟服務，停在原地並明確列出 drift，交給 operator 裁決。這與 runbook 現有的規則相同。
- **verify PASS 但 loaded↔installed 不一致**：視為失敗，自動 rollback。

## 7. Hard-crash recovery

`cortex upgrade --recover` 對應 runbook §6：

- 從 maintenance snapshot 讀出 plan sha 與 receipt path。plan sha 不再由人工輸入；snapshot 本身就是 root-owned、plan-bound 的 durable 記錄。
- 舊 helper 還活著時拒絕執行。
- （#1270 owner 裁決 2026-10-06）先判斷能否收尾：snapshot 綁定的 receipt（只剩 marker 時為 receipt chain 判定、由同一份 plan 產生的 effective receipt）為 applied＋qualified、三個 service 都 active、Manager／Monitor 的 loaded runtime 與它一致時，只清 snapshot 與 marker、不 rollback，report 記為 `finalized`。任何一點不成立或無法證明才走下一點，輸出帶 `"action": "rolled-back"` 與原因。
- 先停止所有 Cortex unit，再以 snapshot 綁定的 receipt 執行 rollback；只有 `restore_safe=true` 時才恢復原本 active 的 unit。
- 最後清除 snapshot 與 marker。

## 8. 測試

- **單元測試**（不需要 root，以 seam 隔離 subprocess／systemctl／網路）：
  - 步驟順序與每一步的參數，包括 plan sha 綁定與 prior receipt。
  - 版本不升時拒絕；非 root 時拒絕。
  - REST digest 與下載檔不符時，在 ingress 階段就停止。
  - 有在飛 job 時拒絕，以及 `--wait-idle` 的等待行為。
  - 各步驟失敗時的 rollback 分支，以及 `restore_safe=false` 時停在原地。
  - credential handoff 的條件，包括 digest 不符與新增 provider。
  - recover 只讀 snapshot。
- **RC qualification 整合**：在 release profile 的容器裡，把現有「apply 後立刻 rollback」的升級演練擴充為完整升級。流程是先裝 prior release，再對同一容器以 candidate 執行 `cortex upgrade`，接著驗證 activate、verify、loaded↔installed 一致，最後再演練一次「verify 後注入失敗 → 自動 rollback → 回到 prior」。
  - release 來源透過 `--repository` 指向容器內的本機假 release 伺服器，或改用 file 來源，提供與 GitHub REST 相同形狀的 tag／asset metadata。

## 9. 安全考量

- 信任來源與現行 runbook 相同：GitHub release REST metadata（annotated tag target、asset digest），加上 qualification manifest 與 bundle 的逐檔驗證。owner 已裁決不額外加簽章。
- 只有 root 能執行。`cortex upgrade` 不提供任何讓 Manager、job 或模型呼叫的入口，也不把 upgrade 註冊成 service。
- 執行 root 指令的是已安裝的 `/opt/cortex/venv`。它負責 ingress 驗證，以及呼叫**封存後的 candidate CLI**。candidate code 只有在 digest 全部驗過、封存之後才會被執行，這與 runbook 的順序相同。
- `--repository` 只接受 `owner/repo` 形狀。實作需界定只有測試情境可以覆寫（例如需要環境變數明示），正式使用一律取 installer 設定的固定來源。

## 10. 文件

- 在 `trust-root-transactional-install.md` 開頭加一段「一般升級：`sudo /opt/cortex/venv/bin/cortex upgrade <版本>`」，原本逐步操作的內容改為「首次安裝與手動操作參考」。
- `trust-root-legacy-adoption.md` 最後補一句：adoption 完成後，之後的升級一律使用 `cortex upgrade`。
- CLI help 要同步更新（R-16）。

## 11. 待實作時確認

- 現行 installer 在 prior-receipt 升級後是否會把 credential 記錄帶進新 receipt（§5）。如果不會，補上 handoff。
- maintenance snapshot 目前的格式是否已足夠讓 `--recover` 不靠人工輸入 plan sha（§7）。如果不夠，補上必要欄位，但不改變 snapshot 的權限模型。

## 12. 實作前調查結果與修正（2026-10-05，依 origin/main 程式碼）

本節取代前面各節中與之衝突的敘述。

1. **憑證 handoff 一定要補，而且現行的手動升級同樣走不通。**
   - `apply_plan(--prior-receipt)` 不會把 prior 的 `credentials` 帶進新 receipt（`new_install_receipt` 從 `[]` 開始），所以 `activate_receipt` 必定以 `missing required credential` 失敗。
   - 照 runbook §4 重新匯入也會被拒：落點檔已經存在，新 receipt 卻沒有 authority（`import_credential` 的 `credential destination already exists without matching receipt authority`）。
   - 現行 RC 的升級演練在 apply 之後就直接 rollback，所以從來沒有發現。
   - **修正**：在 `core` 新增 `inherit_prior_credentials(receipt, prior_receipt, *, backend)`。只接手 prior receipt 已記錄的列，條件是 `(principal, provider)` 相同、由新 plan 推得的落點與 prior 相同，而且落點檔通過與 `backend.validate_credentials` 相同的檢查：regular file、nlink 1、uid／gid 正確、權限 0600、sha256 相符。
   - 繼承來的列加上 `inherited_from: <prior receipt_id>` 標記。新 receipt rollback 時，`rollback_credentials` **不得刪除**繼承列的落點檔，因為那是 prior 的憑證；`_receipt_restore_safe` 把「只剩繼承列」視為可安全還原。
   - `InstallReceipt.load` 的 credentials key set 要放寬，接受 `inherited_from`。
2. **目前生效中的 receipt**：沒有現成函式。`service status` 以 mtime 取最新一份，rollback 之後會挑錯。
   - **修正**：新增 `effective_receipt(state_root) -> InstallReceipt`，依 receipt chain 判定：在 receipt 目錄中，找出 `state=="applied"`、`qualified is True`、而且沒有任何「applied 且 qualified 的子 receipt」以 `parent_receipt.receipt_id` 指向它的那一份。必須唯一，否則停止。之後與 `cortex service status --system --install-receipt <該份>` 的 loaded↔installed 交叉核對。
   - `parent_receipt` 的比對沿用 `legacy_purge._successor_qualified_at` 的寫法。
3. **release ingress**：runbook §1 目前全部是 bash 加 python heredoc，而 `qualification/` 沒有打包進 wheel。
   - **修正**：在 `paulsha_cortex/trust_root/install/release_ingress.py` 實作，逐項移植 runbook §1 的檢查：REST metadata、asset digest、qualification manifest、archive topology、解壓、bundle 逐檔驗證（含 owner、mode 與清單完全一致）、`--copies` 離線 venv、tree sha。
   - GitHub REST 以注入的 fetcher 存取，方便測試。
   - 版本由 plan 的 `candidate.wheel.path` 檔名解析，比較時只接受 `MAJOR.MINOR.PATCH`，不引入 `packaging` 依賴。
4. **activate 之後的 rollback 依設計不是 restore-safe**（`trust-root-legacy-adoption.md` L130：新服務啟動後寫入的 runtime state 會被視為 unknown 並保留）。
   - **修正 §6**：
     - **activate 之前**失敗（ingress、plan、apply、credential handoff）：自動 rollback，並依 snapshot 恢復原本的服務。這是可以保證的自動還原。
     - **activate 之後**失敗（verify FAIL、loaded↔installed 不一致）：執行 rollback。若 `restore_safe=false`，就停在原地，report 與終端機明確列出保留的 unknown state，以及接下來該怎麼做：用 `cortex upgrade --recover`，或交給 operator 裁決。工具**不會**自動啟動 prior 的服務。
   - spec §8 的 RC「verify 後注入失敗 → 自動回到 prior」改成兩件事：
     - RC 驗證「activate 前注入失敗 → 自動回到 prior 並恢復服務」；
     - activate 之後失敗的行為，以單元測試驗證「停在原地並列出保留狀態」。
5. **RC 只有單一 candidate，版本相同**，而且 release profile 跑在 `--network none`。
   - **修正**：新增兩個僅限測試的覆寫，只有在環境變數 `PSC_UPGRADE_QUALIFICATION=1` 明示時才接受，否則參數直接被拒：
     - `--release-source <dir>`：讀本機目錄中與 GitHub REST 同形狀的 tag 與 asset metadata，以及 asset 檔本身；
     - `--allow-same-version`：允許升級到相同版本。
   - `--prior-receipt <path>` 也只在同一個環境變數下開放，供 RC 指定不在 canonical 目錄的 prior receipt。
   - 正式部署一律從 GitHub 公開 repo 抓取，只能升級到更高版本，用 `effective_receipt` 定位 current receipt。
6. **lease 與停服務**：runbook 用 bash 停服務，`lease` 子命令走 stdin 協定。
   - **修正**：upgrade 在 process 內直接使用 `cli._maintenance_lease`、`_service_snapshot`、`_stop_current_services`、`_restore_snapshot_services`，不經 stdin 協定。
   - snapshot 的格式不改：它已經有 plan sha、receipt path、service pre-state。`--recover` 由 `plan_sha256` 推出 durable plan `/var/lib/cortex-installer/plans/<sha>.json`，再呼叫現有 `_recover_command` 的邏輯。
7. **host overlay**：plan 只記 overlay 的 digest。adoption 之後，持久檔中若還留著 `legacy_adoption` 區塊，plan 會要求 `--legacy-inventory`。
   - **修正**：upgrade 讀 overlay 時去掉 `legacy_adoption` 區塊，再產生 plan。這個區塊本來就不納入 digest，所以 digest 不變。
8. 改動 `qualification/` 或 `trust_root/install/` 會觸發 release gate 的 legacy-adoption RC 要求（`release_gate.LEGACY_TRIGGER_PREFIXES`），0.1.13 發版時要兩個 profile 都跑。
