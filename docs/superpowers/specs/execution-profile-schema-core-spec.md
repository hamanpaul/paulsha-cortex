---
status: accepted
work_item: execution-profile-schema-core
---

# Execution profile schema／key core 規格

## Authority

本件是 [#835](https://github.com/hamanpaul/paulsha-cortex/issues/835) T01/T02 的獨立純契約切面，
parent #829。依 root 採納的 schema-core 優先裁決及 [PR #848](https://github.com/hamanpaul/paulsha-cortex/pull/848)
head `be59f769786eeee2e1f97e073c722d8e49272653` 的完整母規格、設計、Todo、審查報告。
獨立 owner 為 [#849](https://github.com/hamanpaul/paulsha-cortex/issues/849)，由本批登錄進件；
尚未 freeze／dispatch。`accepted` 是規劃內容狀態，不是產品／模型資格授權。

## Requirements

### R1 — 唯一 production 範圍是新純模組

未來只新增 `paulsha_cortex/coordinator/execution_profile.py`，提供 descriptor、profile／requirements
資料驗證、immutable value、serializer 與 canonical key 純函式。只使用 Python 標準函式庫，
輸入為 caller 明示交入的資料；不讀環境、檔案、clock、catalog／registry，不跑 subprocess／network。
不得修改現有 loader、launcher、resolver、workflow、registry、CLI command 或 package `__init__.py`。
測試可由正常 module import 使用公開 API，但這不代表現有 routing 已消費新契約。

### R2 — 版本化 descriptor 與原生 effort

v1 descriptor／profile 描述 adapter／協定及版本、model、loadout、toolset、sandbox／權限、toolchain 等
執行條件及 effort 資料語法；名稱／值由輸入資料提供，不建立全域 model／agent／effort 名單。
原生 effort 支援 string、integer／finite number、結構化 object／array；descriptor 可約束其
值域／欄位／型別，新增原生值只改 descriptor，不改中央產品分支。不同 adapter 同名 effort 不等價。
本件是明定且有界的資料語法，不是完整 JSON Schema 引擎、不執行 descriptor 內的程式碼。

### R3 — 三層資料不相互猜填

requested、resolved、observed 是各自標記 plane 的獨立紀錄；caller 對每筆提供對應 descriptor，
不能將 requested 覆蓋 observed，亦不能因兩者值不同而抹掉實際觀測。
core 只驗資料格式，不選候選、不套 provider 預設；resolved 的 default 必須由上游明示解析並留來源。
每個條件使用明確 known／unknown 狀態，unknown 保留原因而不帶假 value；effort 的 not_applicable
僅在 descriptor 明示無該設定時合法，不能等同 missing／unsupported。
role／最低品質／pin／independence 是分立的 task requirements 資料，不是本 core 的授權結論。

### R4 — Key plane 與實際條件分立

request/resolved/observed record key 各有版本及 domain separator；actual-condition key 只能從
observed 的完整執行條件構造，不能從 request/resolved key 改名取得。
effort、adapter／協定／runtime 版本、model/revision、loadout/toolset、sandbox／權限或 toolchain
條件改變時，actual key 須不同。必要觀測 unknown 時回 `key=None` 與缺欄位原因，不產假完整 key。
core 可檢查 provenance reference 的結構，但不能驗其外部真實性；有 key 也不代表 measured／qualified。

### R5 — 精確 canonicalization 與排除項

canonical bytes 跨 mapping 順序、process／hash seed 穩定；明定集合與有序 array、number／string、
integer／float 的語意，拒絕 NaN／Inf、非法 Unicode、重複 JSON keys、循環／越界資料與未知欄位。
Design D4 的完整欄位 projection、typed-array tags／arity、JSON token 型別、字串 escaping、
NUL／uint64 length framing 與完整 profile golden 是 normative v1，不得另選看似等價的 node shape。
Design D5 的 depth/nodes／完整 typed semantic bytes 計數與獨立 raw transport guard 同樣 normative，
不得用 serializer 排版決定 64 KiB 裁決；超界在有界讀取／遍歷時拒絕，不完整 decode 後才檢查。
metadata 的時間戳、價格／pricing provenance、report／approval receipt reference 不進 capability key。
descriptor 的可用模型／effort 清單擴充、發現時間或來源 metadata 不改既有相同執行條件的 actual key；
有執行語意的 adapter／協定版本則是條件，不能為了穩定而剝除。

### R6 — 不遷移 state、不代替政策

v1 strict parser 不猜測缺失／未知 schema version，不把舊 identity、envelope 或 manifest 自動升格。
pure serialize/parse 是新資料的 round-trip，不是既有 state migration／restart recovery。
只有具可信原 schema／policy snapshot 的 frozen legacy run 可保留原可證語意；無者維持
legacy/unversioned/unknown，需正式 authority 重新接受才可切新版。這條 stateful 行為由母件後續
binding/migration child 實作，本 core 不接觸舊檔、run、chain、attempt、evidence 或 receipt。
未來 schema 升版必須明示新 parser／key 版本與相容政策；未知版不得默默當 v1，舊 key 不回寫重算。
此升版指 core wire/encoding 契約；adapter protocol/version 等既有欄位的新值仍為 descriptor-only
合法擴充，沿用 core v1 並自然得到不同 actual key，不需因名稱／版本新值重改中央產品。

## Invariants

- I1：唯一新 production 模組、純資料輸入輸出；不啟動 runtime 或自動接線。
- I2：版本／型別／必填欄位嚴格驗證，缺版本與未知版本不猜填。
- I3：原生 effort 由 descriptor 表達，不建立全域等價排序或產品名單。
- I4：requested/resolved/observed 分立，unknown／not_applicable 不冒充 known。
- I5：record key 與 actual key domain 分離，canonical bytes 不依 process／mapping 順序。
- I6：實際執行條件變動可識別，不因同 model 名稱而借用其他配置 key。
- I7：價格／時間戳／外部 receipt 與清單 metadata 不污染能力 key。
- I8：必要 observed 缺失無完整 actual key；格式合法／有 key 不等於資格或授權。
- I9：輸入／回傳值不可藉 alias mutation 改寫已解析紀錄，解析有界且錯誤不洩露原始值。
- I10：無 state migration、歷史回寫、模型／角色授權或真實 producer 已接通的假宣稱。

## Acceptance Criteria

- [ ] C01：虛構 adapter/model descriptor 下，string、新原生 string 值、integer、finite number、nested object/array effort 都可依資料擴充；公開 API 無產品名稱分支（I2、I3）。
- [ ] C02：同一請求的 requested／resolved 與不同的 observed 分筆 round-trip，不互覆；unknown 與合法 not_applicable 保留，錯誤 not_applicable 拒絕（I4）。
- [ ] C03：固定有效紀錄依 Design D4 重算完整 actual／observed canonical bytes 與完整 golden key；不同 process／hash seed、mapping 插入順序及集合排列得到相同結果，ordered array 次序改變則不同。int 1／float 1.0／string "1"／+0.0／-0.0 的五個完整 profile key 逐筆吻合既定 vector，不能用其他 tags／shape／escaping／framing 代替（I5）。
- [ ] C04：逐一 perturb effort、adapter/id/version/protocol、model/revision、loadout/toolset、sandbox/permissions、toolchain；每個有效條件變動都改 actual key，三層 key 不互用（I5、I6）。
- [ ] C05：只改價格、pricing provenance、時間戳、外部 evidence reference 或擴充 descriptor 其他允許值，既有條件 key 不變；不影響執行的欄位有明確排除表（I7）。
- [ ] C06：任一必要 observed 欄位 unknown 時 actual key 不可用，原因精確；格式合法／全 known 仍不產 approved／qualified／permission grant，fake provenance 不冒充已驗真（I8、I10）。
- [ ] C07：未知／缺失／bool schema version、缺欄位、多餘欄位、escape 解碼後重複 JSON key、錯原生型別、bool 冒充 number、NaN/Inf／浮點 overflow、非法 Unicode、越界／循環資料都明確拒絕；整數 token 不先轉 double，float token 採 D4 的 binary64 rounding／signed-zero 規則，合法 surrogate pair 與 exponent 正例可重算（I2、I3、I9）。
      D1 reference grammar 正負例與 D5 depth 16/17、nodes 4096/4097、semantic bytes 65536/65537、raw text 1048576/1048577 均成對驗；合計 descriptor/profile、預解析路徑與 transport 界內不同 encoding 同裁決，超界提早中止。
- [ ] C08：解析後修改 caller 原 dict/list 或 serializer 回傳的副本，已解析內容與 key 不變；API 不改輸入，錯誤只含 code／field locator 而非原始敏感值（I9）。
- [ ] C09：以公開 module API 的 consumer fixture 驗單筆 schema/key 契約，另證明無檔案／環境／clock／subprocess／network 依賴；既有 production consumers 未改，不宣稱 routing 或 PatchMUD 已接通（I1、I10）。
- [ ] C10：新 v1 資料在全新 process round-trip 穩定；缺版本 legacy payload／未知未來版不自動 migrate，舊資料 fixture 原 bytes 不變；更新 descriptor 值域是合法擴充，變更 schema 協定須明示版本（I2、I10）。
      新 adapter protocol/version 值仍 core v1／descriptor-only 且 key 改變；只有 core wire/encoding shape 改變須新版，兩者不可混為一談。

## Parent Coverage

| 母件 AC／Tasks | 本 child 的精確覆蓋 | 留在母件／其他 owner 的必要交付 |
|---|---|---|
| A1／T01 | C01/C02 的 descriptor 與三層 schema | 實際 resolver/launcher descriptor-only 接線，母 T03/T04 |
| A2 | C07 只拒絕資料不支援的值；core 本身無 spawn | argv/terminal/usage/quota/cancel/timeout/sandbox conformance，母 T03/T08 |
| A3 | requirements 保留、C06 不自授權 | unknown-role 語意 #581；pin/品質/independence/Trust Root 真 gate，母 T04/T09 與 #842 |
| A4／T02 | C03–C08 的 key／unknown／metadata 全部純契約 | 真 observed producer 與 qualification 採信，母 T06、#581/#842/PatchMUD #37 |
| A5／T02 | C07/C10 新 schema 與未知／舊版拒收 | frozen run／state migration、CAS/crash、歷史不變、無 snapshot 的正式重新接受，母 T05/T10 |
| A6 | C09 公開 API 的離線契約 fixture | 真 production consumer/producer、既有 routing 回歸與 installed/live，母 T11/T16 |

## Dependencies

本 core 的 schema/key → [#842](https://github.com/hamanpaul/paulsha-cortex/issues/842) qualification
發布 consumer，後者不等整件 #835 close。#842 獨立擁有 report→candidate→human-review receipt→roster、
撤銷／有效期／CAS／migration；真人政策要求的 receipt 不由本件批准。
#581 保留 doctor planning 身分、unknown-role、非 dispatch provenance、doctor overlay 公開 API、
eval producer 五項；[PatchMUD #37](https://github.com/hamanpaul/paulsha-patchmud/issues/37) 只供
外部 CLI/file schema／immutable fixture，零 runtime import、不實作上游或重跑 benchmark。
本 child 可獨立測純契約，不表示母件或 #842 的 production／live 驗收完成。
