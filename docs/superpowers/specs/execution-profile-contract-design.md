---
status: accepted
work_item: execution-profile-contract
---

# Execution profile 契約設計

## Scope

依 [Spec](execution-profile-contract-spec.md) R1–R6／A1–A6；本件為完整母範圍設計，
不是 Red 自動可執行的 build unit。所有下述資料型別／seam 是待實作設計，不宣稱 source 已存在。

## Decisions

### D1 — 以版本化資料為中心，不擴大全域產品枚舉

新增獨立純契約模組（建議 `coordinator/execution_profile.py`），定義 descriptor、
task requirements、profile 三層紀錄與 capability observation 的嚴格型別／serializer。
descriptor 由既有 model identity loader 的公開 seam 載入；保留 packaged／operator overlay
優先序與來源，不建立第二套身份 registry。舊 identity 的 `(executor, model_id)` 僅是相容索引，
不能充當新版能力 key。新增 adapter 是受控程式擴充，不允許任意 descriptor 載入未受信任執行碼。

descriptor 至少宣告 schema version、adapter id／協定版本、可解析 model id、原生 effort schema、
工具／sandbox／權限 capability 與 adapter/loadout/toolchain 版本來源。
effort 保留原生字串或 descriptor 明確定義的數值／結構型別，不轉成全域低中高分數；
省略 effort 時 resolved 記錄 descriptor 的明示 default，provider 未回報時 observed 仍 unknown。
unknown adapter/model、未知 schema、重複／矛盾 descriptor、非法 effort 均在 spawn 前拒收。

### D2 — Canonicalization 只處理實際執行條件

定義含 schema version 的 canonical payload，以固定鍵排序、UTF-8 與無歧義型別編碼序列化；
工具／權限集合先去重排序，原生 effort 的有序結構不任意重排。字串 ID 不擅自大小寫折疊；
拒絕未宣告欄位型別、非有限數值與含憑證的資料，不將機器絕對路徑納入可分享 key。
digest 有版本化 domain separator，分別命名 request key、resolved key、actual-condition fingerprint；
不同 evidence grade 不可互用，即使其已知欄位相同。

actual payload 綁 adapter/協定版本、observed model/revision（已知時）、原生 effort、loadout/toolset、
sandbox／權限條件與 toolchain 版本；缺觀測欄位保存 unknown 及原因，不產出「完整實測」標記。
時間戳、價格、pricing provenance 留在報表 metadata，不進 capability fingerprint。
資格另外由 #842 綁 execution fingerprint、role/deck/coverage、report 與 human-review receipt；
不把 pricing 加進任何能力 key，不把本件 execution hash 當完整 qualification hash。

### D3 — Adapter conformance 與既有安全 wrapper 分層

新增公開 adapter protocol／受控 registry（建議 `coordinator/execution_adapters.py`），以 descriptor
查 adapter，adapter 負責 native argv、option 支援、terminal/usage 解析、quota capability 聲明與
cancel/timeout 接口。共用 wrapper 保留 worktree、Trust Root、spool／last-message、安全工具政策、
process lifecycle 及審計責任，adapter 不能因能力宣告而越權。
quota capability 是 supported／unsupported／unknown 及 schema/source，不是餘量或預算判斷。

`launcher.SubprocessLauncher`、`coordinator.cli._resolve_launcher` 與各種 `as_*` specialization
消費相同 resolved profile；既有 clone 已轉交 effort，不把它列作已知缺陷。
`planning_runtime._planning_argv`／`_invoke_json` 是另一個生產入口，須接同一 adapter 契約，
保留純 JSON、hermetic config、零工具／read-only、disposable sandbox 與 last-message 落點。
review-only／commit-required／write-forbidden 仍依 card contract 決定，不依固定 Persona 名單推測。
新增 model/effort 只改 descriptor；新 runtime 僅增加 adapter 實作、descriptor 與 conformance，
central resolver／quota／workflow／reporting 以協定消費，不追加產品名稱 if/elif。

### D4 — 選擇與 admission 不得互相稀釋硬條件

沿用 `model_resolution.rank_candidates` 的分層順序與 credential／權限檢查，
在 `manager._workflow_identity_candidates_for_persona`、`_runtime_preflight_gate` 及最終
`_dispatch_workflow_card` 的選擇／launch 接縫傳遞完整 profile requirements。
`manager_daemon` 與 `autonomy.dispatch_ready` 的 launcher factory 不再只轉交 executor/model pair。
保留 generic dispatch 與 workflow lane 的各自 owner／preflight，不只接一條示範路徑。

pin 固定目標但不豁免其適用政策的品質要求。既有 frozen run 只依可證的原 schema／policy snapshot 重讀，
不因套入新版預設而追溯改變其語意，也不升格宣稱具備新版 profile 資格。新 run 或經正式
authority 流程明示切換新版者，若 exact profile／最低品質不可證則阻塞，不修改 frozen chain
讓它看似選到其他模型；active legacy job 的安全處置仍須走既有 owner-aware action。
若歷史 run 沒有足以重建當時 schema／policy 的可信 snapshot，保存原件並明示
legacy/unversioned/unknown；不從今天的預設、目前版本或猜測的歷史版本補值，不回寫原 chain，
不據此宣稱恢復其可執行政策或繼承品質 bypass。切到新版必須正式、具 authority 的重新接受，
不是單純 reload、升級安裝或修改展示標籤。root 對本設計的接受不等於該 run 的重新接受 authority。
unknown-role 由 #581 收口 public role 解析 seam，
#835 consumer 必須驗其 fail-loud 契約，不在別處保留 catch-all build 預設。
資格查詢使用 #842 的 exact profile/role/coverage／有效 receipt 契約；該依賴未交付時可用 fixture
開發，但不可接舊 pair-key roster 就宣稱新版 qualification 生效。
#534 的人工核可政策保持權威，descriptor 登錄、probe 成功、測量 pass、quota 足夠都不是核可。

### D5 — 相容欄位不變，新 binding 是 versioned sibling

`workflow._validate_model_chain_override` 與 `_validate_model_chain_resolution` 現有嚴格鍵集合
不得直接塞入未宣告 profile 欄位；新增獨立版本化 profile binding，保留原 override/resolution
與舊 reader 所需投影。`WorkflowRun`、JobRegistry 與 launcher invocation 的 serialization 必須
共同驗證同一 resolved key／descriptor revision；不要為此建立第二個 WorkflowRun 真值源。

新 attempt 在現有 owner-aware registry 寫入邊界綁定 immutable requested/resolved profile；
已觀測資料以該 attempt 的新 evidence 引用保存，不覆寫舊 evidence。registry 原子替換與其
既有鎖／交易邊界沿用；寫入中斷後只能讀到一致的舊／新 binding，缺依據則 unknown／阻塞。
凍結 manifest／chain 只讀不改，legacy 缺欄位不回填虛構 profile；新的 binding 可引用舊 receipt
但不重算覆蓋它。unknown schema 明確診斷，不用「忽略新欄位」冒充前向相容。
#581 擁有通用非 dispatch provenance 缺口；本件只確保新增 profile binding 經該共用 seam 傳遞。

### D6 — Producer 與 qualification 的接合，不越過授權邊界

沿用 `envelope_mapping.map_report_to_envelope`、`model_profile` 現有 CLI/file producer seam；
現有六欄 fingerprint 不是含 effort/loadout/toolchain 的完整 actual-condition key，不能直接
更名宣稱已符合。#835 提供新 key 對照，#581 接 producer，#842 管資格發布／撤銷／有效期／CAS。
PatchMUD #37 immutable fixture 必須帶來源 revision／schema／digest，由實際 parser 消費；
本地自行製造 fake report 只驗 Cortex contract，不假冒上游交付或完整 encounter coverage。
若 observed effort、adapter/loadout、role 或 coverage 不符／unknown，保留證據且不授予新版資格。
live 若要求真人批准，須 exact qualification 的合法真人 receipt；本 intake 與模型產出的
審查文字不屬該批准。既有歷史 attempt/evidence 不因 producer 升版或 qualification 撤銷而重寫。

## Validation

| Spec 驗收 | 最小 fixture 與可驗證邊界 |
|---|---|
| A1 | 虛構 adapter 協定下兩個模型與新 native effort，descriptor-only 擴充；exact requested/resolved 與中央路徑差異檢查 |
| A2 | fake adapter＋受控本地子程序，Event/barrier 確認 argv、terminal、usage、quota capability、取消／timeout、sandbox；unsupported spawn_count=0 |
| A3 | 各一個 unknown-role／錯 pin／不足 qualification／同 domain／無效 Trust Root fixture；quota 足夠仍零 launch |
| A4 | 配置欄位逐項 perturbation，effort/version/loadout/toolchain 必變，價格／時間／mapping 順序不變；unknown 不補值 |
| A5 | legacy fixture＋frozen chain＋隔離 JobRegistry，序列化重讀／重啟／寫入 failpoint；歷史 bytes/digest 不變，未知版與半 descriptor 明確失敗 |
| A6 | 真正 production resolver/factory/launcher/planning parser 搭 fake subprocess 先驗 contract，再驗 PatchMUD immutable fixture；真 launcher/install/live 獨立 gate |

A5 另含缺可信 schema／policy snapshot 的歷史 run：重啟後仍是 legacy/unversioned/unknown，
未填入目前／猜測版本，原 chain bytes/digest 不變；無重新接受 authority 的新版切換請求拒絕。
測試 receipt 只驗 action contract，不冒充現場重新接受或真人 qualification 核可。

先使用現有 `tests/test_model_resolution_chain_534.py`、`tests/test_per_work_model_chain.py`、
`tests/test_model_identities.py`、`tests/test_model_chain_profile_resolution.py`、
`tests/test_coordinator_launcher.py`、`tests/test_coordinator_agy_launcher.py`、
`tests/test_planning_runtime.py`、`tests/test_card_contract_sandbox_mode_716.py`、
`tests/test_trust_root_hardening_profile_643.py`、`tests/test_model_profile_cli.py` 作回歸入口。
新增 profile schema／adapter conformance／production binding tests 由 Cortex 實作，
測試命名不是既有成果。純函式、負例與 restart 先於任何需授權的實際 provider 呼叫。

## Bounded Dependencies

#842 qualification 與 #581 producer／role/provenance 在整合 gate 前須提供契約和證據；
其前可在本件用明示 fixture，不把未完成依賴填成 pass。PatchMUD #37 的 immutable fixture 與
真人批准 receipt 是對應 gate 的外部輸入，不能靠重跑付費 benchmark 或 agent 自行批准替代。
#831 sizing 修正後仍 Red；#833 自動分解尚未交付，此處僅提供人工拆分候選，不假造 runtime 能力。
這些依賴不阻止 schema／adapter 的離線開發準備，但阻止整件完成及未授權 live acceptance。
