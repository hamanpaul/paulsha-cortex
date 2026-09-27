# 需求交付總帳契約（#845）

`refine-requirements-v1.json` 是 Cortex refine R01–R14 的版本化 requirement manifest。每列固定 requirement revision、accepted plan 原文 SHA-256、acceptance criterion、必要證據階段與 owner work refs。新增需求若缺 acceptance criterion 或 evidence policy，validator 拒絕載入。source root 內對應文件的 bytes 必須符合 manifest hash；改需求時應更新 revision 與 manifest，不沿用舊 mapping。

`validate_manifest` 要求呼叫端提供 `authority_root`：先以 `authority_ref.locator` 讀出實際 accepted plan／spec 內容，核對 `authority_ref.revision` 即該內容的 SHA-256（不只檢查非空），再解析其 requirement inventory 表格，鎖定 requirement id 與標題集合。manifest 在沿用同一 accepted revision 時，需求識別碼缺漏或多出、標題被竄改、acceptance criteria 被清空，都會 fail closed；驗收條件本身的操作化措辭仍由 manifest 定義，不強求與計畫散文逐字一致。

## Evidence snapshot 與逐階段驗證

evidence producer 以 `cortex/requirement-evidence-snapshot/v1` 提供 `captured_at`、單調 `snapshot_revision`、mapping 與 scoped waivers。每個 mapping 至少綁定 requirement/revision、acceptance ID、repo、work、run、workflow step IDs、source generation、candidate SHA、PR、OpenSpec change、Todo paths、execution profile/config/policy revision 及 CompletionRecord locator/hash。snapshot 是候選關聯資料，不是通過證據；consumer 會逐一重新驗證：

| 階段 | 採信來源 |
|---|---|
| source | manifest 指定 locator 的可信 source-root bytes 與 SHA-256。 |
| test / review | 正式 CompletionRecord domain validator、hash-bound verification/review receipts、candidate 綁定、fresh WorkAuthority 與不同 reviewer independence domain。verification evidence 的 run 層級狀態（`reviewing`／`verified`）只表示整體驗證通過，不可直接推定覆蓋某 acceptance criterion；test 階段只採信 `details.tests` 中明確以 `acceptance_ids` 綁定該 criterion且 `status: passed` 的測試，未綁定或未過即為該 criterion 的 test gap。 |
| merge | 以 WorkAuthority 授權的 PR 重新讀 GitHub closure facts，交既有 `evaluate_remote_closure` 驗 exact head、merge ancestry、issue 是否 closed、OpenSpec；只讀 API 查詢，不執行 closure。`evaluate_remote_closure` 本身只看 issue 目前是否 closed，不驗證是不是被這個 PR 關閉、也不把 Todo 勾選狀態當結案門檻（該語意留給 #808/#810 的 Todo/issue 擁有者，不能改共用 gate）；本 consumer 在此之上另外加嚴：mapped issue 必須出現在這條 PR 實際 `closingIssuesReferences`（`RemoteClosureFacts.closing_issues`），mapped todo.md 遠端內容必須全數勾選完成（`RemoteClosureFacts.todo_complete`），任一不成立即 `failed` 並留下具體 gap，不得只憑 issue 目前 closed 或既有 gate allowed 就記 verified。CompletionRecord 的 `target_ref_sha` 是合併當下捕捉、不可變的歷史快照；其他 PR 之後推進 default branch 是正常事件，consumer 不要求它與「目前」remote default head 相等，只沿用 `evaluate_remote_closure` 已驗證的 merge ancestry（merge commit 是目前 default head 的祖先），避免已完成的交付因之後的正常推進而退化。merge evidence 的 digest／身分只綁這筆交付不可變的事實（merge commit、PR head、PR number、mapped issues、todo 完成狀態）；「目前」default head 只作為驗證輸入，回傳值另外附上 `target_sha` 供報告參考，但索引持久化與同 generation 內容比對前一律剔除，因此其他 PR 之後再推進 default branch 不會被誤判成內容改變。 |
| installed | 消費 #841 既有 `cortex service status` loaded-runtime projection，以 service declaration 的實際 unit PID 比對 loaded artifact digest/source revision；再核對 service/instance、profile/config revision、target 與 Trust Root。CLI 只從既有 Manager coordinator root/Monitor state root 讀取，忽略 snapshot 的 root hint。 |
| live | 驗 `cortex/live-canary-receipt/v1` hash、scope、target、期限、核可 authority 與 reviewer/canary 獨立性；通過後交 production `live_receipt_validator`（`paulsha_cortex/coordinator/live_receipt_validators.py` 的 `make_governed_live_receipt_validator` 工廠，`porcelain/delivery.py` 以 CLI 的 `--source-root` 與 delivery evidence root 建立，已接在 `cortex delivery gaps／reconcile`）依 `receipt["kind"]` 封閉登記表逐 kind 檢查；未登記 kind 一律 fail closed。independence 檢查的 `canary_domain` 不是 receipt 自報值——`_verify_live` 改用注入的 `live_receipt_domain_deriver`（production 為 `live_receipt_validators.derive_canary_domain`，`inspect_delivery`／`reconcile_delivery` 的 `live_receipt_domain_deriver` 參數，`porcelain/delivery.py` 已接線）依 `receipt["kind"]` 從已驗過的 evidence 導出；receipt 若仍自報該欄位，必須與導出值完全相同，否則拒絕；無法導出（kind 未登記、evidence 缺對應欄位）一律 fail closed。 |

### Live receipt 封閉登記表（#845 A04）

`make_governed_live_receipt_validator(*, source_root, evidence_root)` 是一個工廠：
`porcelain/delivery.py` 以 CLI 的 `--source-root`（checkout 根目錄）與 delivery
evidence root（目前是 Manager coordinator root）呼叫一次，回傳的 callable 才是
`_verify_live` 實際使用的 `live_receipt_validator`——這個 callable 仍然只吃
receipt 本身（符合 `_verify_live` 既有呼叫慣例），`source_root`／`evidence_root`
是工廠建立時由呼叫端決定、receipt 內容無法操控的信任邊界。回傳的 validator 只
接受 ``receipt["kind"]`` 命中下列封閉登記表的 receipt；其他任何 kind（包含空值／
型別不符）一律回傳 `False`（fail closed），不得因為看不懂就放行。任何解析例外也
一律視為未通過，不外露例外內容。receipt 本身的 schema／hash／target／期限／
authority／independence 已由 `_verify_live` 驗過，validator 只再檢查
``receipt["evidence"]`` 這份同一份已 hash 綁定內容（deployment-canary 這個 kind
另外還檢查 ``receipt["canary_target"]`` 與其指向的 evidence 目錄）：

| kind | 內容與綁定規則 |
|---|---|
| `cortex/deployment-canary-qualification/v1` | `receipt["evidence"]` 是完整 `qualification.json`（`schema_version: 2`、`profile: deployment-canary`、`status: passed`），以 `require_canary_profile=True` 呼叫 `qualification/validate.py` 既有 `validate()` 做結構與 fail-closed 判定（provider 需求、canary-only tests／artifacts 等），不另寫一套；`evidence.candidate_sha`／`evidence.wheel.sha256` 必須精確等於這條需求 claim 的 `target.candidate_sha`／`target.artifact_sha256`。額外要求 `receipt["canary_target"]`（`repository`／`work_id`／`issue`／`evidence_directory`）：`repository` 必須等於已驗證的 `target["repo"]`；`evidence_directory` 必須**精確等於** `live_receipt_validators.deployment_canary_evidence_scope(target)`（見下方「target 欄位的可證明性」），解析出的目錄交給 `validate()` 的 `evidence_root` 參數——`validate()` 會逐檔核對 artifact-inventory 宣告的檔案是否存在、內容 sha256 是否相符、evidence tree 是否有多餘或缺漏檔案，並比對 evidence 自報的 `dispatch-closeout.json` repository／work_id／issue 是否與外部帶入的 `canary_target` 一致。`qualification/validate.py` 由工廠以 `importlib.util.spec_from_file_location` 從 `<source_root>/qualification/validate.py` 動態載入（路徑須在 source_root 之下且非 symlink），不再依賴一般 `import qualification` 撞運氣命中 repo 根目錄——已安裝的 wheel 不打包 `qualification/`，缺檔或載入失敗一律 fail closed。任一項缺失，或 receipt 竄改（仍由 `_verify_live` 的 sha256 綁定防止），都拒絕。 |
| `cortex/task-memory-live-canary/v1` | `receipt["evidence"]` 是 #857 task-memory canary（`cortex task-memory canary --evidence-path` 產物）：`passed: true`；`executor.id`（非空字串，`derive_canary_domain` 的導出來源）；`content_retrieval` 與 `paths.{context-delivered,snapshot-ready,note-fetch}` 三個 delivery path 各自 `attempts ≥ 5` 且 `successes/attempts ≥ 0.95`（`success_rate` 必須與 `attempts`/`successes` 一致，不可只填數字不驗算）；`negative_controls` 需覆蓋 `permission-denied` 與 `cross-scope-rejection` 兩個固定案例且 `status: passed`；`cross_project` 至少兩個不同 repo 且 `status: passed`。`evidence.target` 必須與 receipt 外層已驗證的 `target`（即這條需求 claim 的 artifact digest／source revision／service／instance／profile／config revision，與同時期 #841 loaded-runtime receipt 綁定同一身分）逐欄位相等；無法綁定即拒絕。此 kind 不使用 `qualification/validate.py`，不受 `source_root` 缺檔影響。 |

未知 kind、上述任一綁定或門檻不成立、embedded 內容格式錯誤，都回傳 `False`，對應
`_verify_live` 的 `governed-live-receipt-validator-rejected` gap（`status: failed`），
不是 `unknown`——因為到這一步 schema／hash／target／期限／authority／independence
都已驗過，內容本身不符是明確 fail，不是「無法判斷」。

#### canary_domain 導出來源（#845 對抗審查第九輪 BLOCKER）

`independence.canary_domain` 不是 receipt 自報字串。`_verify_live` 改用注入
的 `domain_deriver`（`inspect_delivery`／`reconcile_delivery` 的
`live_receipt_domain_deriver` 參數，production 為 `live_receipt_validators.
derive_canary_domain`，`porcelain/delivery.py` 已接線）依 `receipt["kind"]`
從已驗過的 evidence 導出：

| kind | 導出來源 |
|---|---|
| `cortex/deployment-canary-qualification/v1` | `evidence.image.digest`（`sha256:` + 64 hex）。`qualification/driver.py` docstring 明示這是複製進候選掛載之前的參考映像，與候選 checkout／builder／reviewer 執行環境無關。 |
| `cortex/task-memory-live-canary/v1` | `evidence.executor.id`（非空字串；本輪新增的必要欄位，代表產生這份 canary 量測的執行者身分）。 |

receipt 若仍自報 `independence.canary_domain`，該值必須與導出值**完全相同**，
否則拒絕；kind 未登記或 evidence 缺對應欄位（無法導出）一律回傳 `None`，視為
independence 檢查失敗，不代入預設值放行。

#### target 欄位的可證明性（#845 對抗審查第九輪 MAJOR）

deployment-canary qualification evidence 測的是「這顆 wheel／candidate 能否
通過 attack matrix 與基本 dispatch」，不是「某個特定 profile／instance 的
部署事實」；它的 schema 只能直接證明 acceptance target 8 個欄位中的
`repo`／`candidate_sha`／`artifact_sha256`（已直接比對 evidence 內容）。其餘
五個欄位——`source_revision`／`service`／`instance`／`profile_key`／
`config_revision`——完全沒有對應內容可比對，若不處理，同一份真正的 canary
evidence 只要改外層 `receipt["target"]` 這五個欄位就能重複核銷不同
profile/instance 的需求，違反 A04 exact target。

修法：`live_receipt_validators.deployment_canary_evidence_scope(target)` 只
取這五個欄位算出 canonical JSON 的 SHA-256；`canary_target.evidence_
directory` 必須精確等於這個 digest（不再是 receipt 自由選擇的字串），因此
真正的 evidence 檔案必須存在於 `evidence_root/<digest>/...` 這個路徑之下
——`evidence_root` 是呼叫端信任邊界（見上方工廠說明），receipt 內容無法寫
入。同一份 wheel canary evidence 換掉這五個欄位後，其唯一合法路徑會跟著改
變；除非攻擊者真的取得 `evidence_root` 寫入權限並把檔案複製到新路徑，否則
無法讓舊 evidence 通過新 target 的檢查——這已經超出單純竄改 receipt JSON 的
威脅模型，是本模組唯一不受 receipt 內容操控的信任邊界。

`cortex/task-memory-live-canary/v1` 的 evidence 完全沒有檔案樹可供內容定
址，目前仍只靠 `evidence["target"] == 外層 target` 的自我一致檢查（`_verify_
live` 已先驗證外層 target 就是這條需求 mapping 的 acceptance target）；這是
已知的較弱綁定，非本輪對抗審查具體指出的項目，於此明確記載，不佇裝已解決。

`owner.work_ids` 的 `owner/repo#issue` 必須與正式 WorkAuthority 的 repo 及 mapped issue 相符；只有相同標籤、PR closed 或別的 work 完成不會覆蓋該需求。每列 coverage 依自身列出的 required stages 計算；缺 stage、failed、stale 或 unknown 都不能 ready。同一次 snapshot 若對同一 requirement／criterion／repo／work／run 邏輯範圍同時含多個 source_generation 的 mapping row（例如較舊 covered 與較新 blocked 同時存在、producer 尚未清理掉舊紀錄），判定該 criterion covered 與否只信這個邏輯範圍內最高 generation 的 row，不是只要任一 row covered 就通過；未選中的較舊 generation row 不影響判定，也不會蓋掉較新事實的 gap 回報。此規則與下方索引跨 mapping generation 守門共用同一套「以最高 generation 為單一真相」邏輯。

## 索引與恢復

索引是 Manager-owned 的唯讀可重建 sidecar，位置由 Trust Root `requirement-delivery-index` 登記；它不取代 WorkflowRun、CompletionRecord、GitHub 或 #841 receipt。reconcile 先讀並重驗可信來源，再核對 WorkAuthority digest，最後以檔案鎖、精確 index revision CAS、同目錄暫存檔、fsync 與原子替換發布。replay 不重寫來源證據；舊 source generation 不可覆蓋較新值——generation 比較以 requirement/criterion（含 repo/work/run）範圍為準、跨 mapping_id 比對，即使 candidate/completion_record 變動換了 mapping_id，從未入索引過的舊 generation 仍不得把已落地的較新 covered mapping 標成 stale 並取而代之；candidate/profile/config/policy/requirement revision 變化只使相符歷史 mapping stale。這個 generation 守門以該邏輯範圍內**已見過的最高 generation（不論既有 row 目前狀態）**為準，不只看 covered rows：即使該範圍最新一筆已經是 blocked／stale，遲到的更舊 generation 仍一律視為過期輸入拒收。若不同 mapping_id 落在同一邏輯範圍且 generation 相同，代表這是對同一份交付的重新讀取而非新一代；重讀結果若比既有 covered 弱（任一原本 `verified` 的階段在新讀取中不再是 `verified`，例如 merge/installed 證據因暫時性讀取失敗而遺失），保留既有 covered、回報 pending／drift，不讓暫時性弱讀取靜默取代已驗證結果。未知的同版本欄位保留於 extensions；未知 future index schema 拒絕自動降版或覆寫。

索引 gap 保存最後 reconcile 的機讀投影。`status` 僅讀這份投影；`gaps` 重新查可信來源並產生即時缺額但不寫索引；`reconcile` 才寫 sidecar。三者都不呼叫模型、不派工、不 merge、不部署、不改 issue，也不關票。waiver 必須精確限定 requirement/revision/acceptance/stage、理由、核可 authority/version、期限與 receipt，並由外部核可 validator 確認；installed/live 永不可 waiver。此 manifest 目前不授權任何 waiver，保留其必要階段。

## Live 操作

本 repo 的 fixture E2E 使用正式 CompletionRecord shape、remote closure facts 與 #841 runtime receipt。`cortex delivery gaps／reconcile` 已固定以 `live_receipt_validators.make_governed_live_receipt_validator` 工廠建立 validator（見上方封閉登記表），producer 交付上表兩種 kind 之一的合法 receipt 即可通過；receipt kind 不在登記表內或內容不符綁定規則時，明確保留 `failed` live gap，不會因為看不懂 kind 就放行。source 測試或本機 checkout 不可當成安裝/實際載入證據。

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
