# 需求交付總帳契約（#845）

`refine-requirements-v1.json` 是 Cortex refine R01–R14 的版本化 requirement manifest。每列固定 requirement revision、accepted plan 原文 SHA-256、acceptance criterion、必要證據階段與 owner work refs。新增需求若缺 acceptance criterion 或 evidence policy，validator 拒絕載入。source root 內對應文件的 bytes 必須符合 manifest hash；改需求時應更新 revision 與 manifest，不沿用舊 mapping。

`validate_manifest` 要求呼叫端提供 `authority_root`：先以 `authority_ref.locator` 讀出實際 accepted plan／spec 內容，核對 `authority_ref.revision` 即該內容的 SHA-256（不只檢查非空），再解析其 requirement inventory 表格，鎖定 requirement id 與標題集合。manifest 在沿用同一 accepted revision 時，需求識別碼缺漏或多出、標題被竄改、acceptance criteria 被清空，都會 fail closed；驗收條件本身的操作化措辭仍由 manifest 定義，不強求與計畫散文逐字一致。

## Evidence snapshot 與逐階段驗證

evidence producer 以 `cortex/requirement-evidence-snapshot/v1` 提供 `captured_at`、單調 `snapshot_revision`、mapping 與 scoped waivers。每個 mapping 至少綁定 requirement/revision、acceptance ID、repo、work、run、workflow step IDs、source generation、candidate SHA、PR、OpenSpec change、Todo paths、execution profile/config/policy revision 及 CompletionRecord locator/hash。snapshot 是候選關聯資料，不是通過證據；consumer 會逐一重新驗證：

| 階段 | 採信來源 |
|---|---|
| source | manifest 指定 locator 的可信 source-root bytes 與 SHA-256。 |
| test / review | 正式 CompletionRecord domain validator、hash-bound verification/review receipts、candidate 綁定、fresh WorkAuthority 與不同 reviewer independence domain。verification evidence 的 run 層級狀態（`reviewing`／`verified`）只表示整體驗證通過，不可直接推定覆蓋某 acceptance criterion；test 階段只採信 `details.tests` 中明確以 `acceptance_ids` 綁定該 criterion且 `status: passed` 的測試，未綁定或未過即為該 criterion 的 test gap。 |
| merge | 以 WorkAuthority 授權的 PR 重新讀 GitHub closure facts，交既有 `evaluate_remote_closure` 驗 exact head、merge ancestry、issue 是否 closed、OpenSpec；只讀 API 查詢，不執行 closure。`evaluate_remote_closure` 本身只看 issue 目前是否 closed，不驗證是不是被這個 PR 關閉、也不把 Todo 勾選狀態當結案門檻（該語意留給 #808/#810 的 Todo/issue 擁有者，不能改共用 gate）；本 consumer 在此之上另外加嚴：mapped issue 必須出現在這條 PR 實際 `closingIssuesReferences`（`RemoteClosureFacts.closing_issues`），mapped todo.md 遠端內容必須全數勾選完成（`RemoteClosureFacts.todo_complete`），任一不成立即 `failed` 並留下具體 gap，不得只憑 issue 目前 closed 或既有 gate allowed 就記 verified。 |
| installed | 消費 #841 既有 `cortex service status` loaded-runtime projection，以 service declaration 的實際 unit PID 比對 loaded artifact digest/source revision；再核對 service/instance、profile/config revision、target 與 Trust Root。CLI 只從既有 Manager coordinator root/Monitor state root 讀取，忽略 snapshot 的 root hint。 |
| live | 驗 `cortex/live-canary-receipt/v1` hash、scope、target、期限、核可 authority 與 reviewer/canary 獨立性；最後仍需正式 live receipt validator。沒有 validator 時為 `unknown`。 |

`owner.work_ids` 的 `owner/repo#issue` 必須與正式 WorkAuthority 的 repo 及 mapped issue 相符；只有相同標籤、PR closed 或別的 work 完成不會覆蓋該需求。每列 coverage 依自身列出的 required stages 計算；缺 stage、failed、stale 或 unknown 都不能 ready。

## 索引與恢復

索引是 Manager-owned 的唯讀可重建 sidecar，位置由 Trust Root `requirement-delivery-index` 登記；它不取代 WorkflowRun、CompletionRecord、GitHub 或 #841 receipt。reconcile 先讀並重驗可信來源，再核對 WorkAuthority digest，最後以檔案鎖、精確 index revision CAS、同目錄暫存檔、fsync 與原子替換發布。replay 不重寫來源證據；舊 source generation 不可覆蓋較新值——generation 比較以 requirement/criterion（含 repo/work/run）範圍為準、跨 mapping_id 比對，即使 candidate/completion_record 變動換了 mapping_id，從未入索引過的舊 generation 仍不得把已落地的較新 covered mapping 標成 stale 並取而代之；candidate/profile/config/policy/requirement revision 變化只使相符歷史 mapping stale。未知的同版本欄位保留於 extensions；未知 future index schema 拒絕自動降版或覆寫。

索引 gap 保存最後 reconcile 的機讀投影。`status` 僅讀這份投影；`gaps` 重新查可信來源並產生即時缺額但不寫索引；`reconcile` 才寫 sidecar。三者都不呼叫模型、不派工、不 merge、不部署、不改 issue，也不關票。waiver 必須精確限定 requirement/revision/acceptance/stage、理由、核可 authority/version、期限與 receipt，並由外部核可 validator 確認；installed/live 永不可 waiver。此 manifest 目前不授權任何 waiver，保留其必要階段。

## Live 操作

本 repo 的 fixture E2E 使用正式 CompletionRecord shape、remote closure facts 與 #841 runtime receipt。受治理環境需先由各工作 owner 提供 snapshot 和正式 live validator/receipt producer；本 CLI 未收到外部 live validator 時會明確保留 live gap。source 測試或本機 checkout 不可當成安裝/實際載入證據。

在有受治理 runtime、有效 Monitor WorkAuthority snapshot、GitHub read authentication 與 producer snapshot 的環境執行：

```bash
cortex delivery reconcile \
  --manifest docs/superpowers/specs/refine-requirements-v1.json \
  --snapshot "$PSC_COORDINATOR_ROOT/evidence/requirement-delivery/source-snapshot.json" \
  --source-root "$(git rev-parse --show-toplevel)" \
  --checkout "hamanpaul/paulsha-cortex=$(git rev-parse --show-toplevel)" \
  --checkout "hamanpaul/paulsha-patchmud=$HOME/prj_pri/paulsha-patchmud"
```

命令會重驗來源並更新衍生索引；producer 尚未交付的 receipt 仍列在 `report.gaps`，不會藉由重建補造成功紀錄。要即時重驗而不改索引，改用相同參數執行 `cortex delivery gaps`。確認最近一次保存的 gap 可用 `cortex delivery status`。
