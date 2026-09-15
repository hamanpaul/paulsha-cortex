---
status: accepted
work_item: quota-observation-schema-core
owner_issue: 866
parent_issue: 836
domain_breadth: 0
state_consistency: 1
invariant_count: 10
artifact_classes:
  - source
  - tests
  - documentation
applicable_contract_rules:
  - R-09
  - R-16
  - R-19
---

# Quota observation schema core 設計

## Decisions

本設計是 `quota-observation-schema-core` 的 standalone normative wire/API contract；所有欄位、variant 和界線以 D1–D9 為準。Owner 為 [#866](https://github.com/hamanpaul/paulsha-cortex/issues/866)，parent #836；R3 fresh independent planning review PASS 後，root 已接受規劃內容，尚未 PR／發布／formal frozen authority／dispatch／qualification。母 #836 的 parser adapters、ledger、shadow／admission 未實作。只有一個預定 production module `paulsha_cortex/coordinator/quota_observation.py`，沒有現有 caller 接線。Spec/proposal 的 I1–I10 與本設計一一相容，spec 與 own proposal、design 與 own design、todo 與 own tasks 必須各自 byte-identical。以下 draft／memory-only accepted 的 sizing 分帳保留為 R3 受審歷史，本次不改數值或契約。

## D1 — Public API、wire primitive 與限制

public API：

```python
parse_unit_definition(payload: dict) -> UnitDefinition
parse_pool_descriptor(payload: dict) -> PoolDescriptor
parse_binding(payload: dict, *, descriptors: tuple[PoolDescriptor, ...]) -> ProfilePoolBinding
parse_observation(payload: dict, *, descriptors: tuple[PoolDescriptor, ...],
                  unit_catalog: tuple[UnitDefinition, ...]) -> QuotaObservation
binding_status(binding: ProfilePoolBinding) -> BindingStatus
freshness(observation: QuotaObservation, *, now_utc_ms: int,
          allowed_clock_skew_ms: int) -> Freshness
event_identity(observation: QuotaObservation) -> EventIdentity
```

四種 record（UnitDefinition、PoolDescriptor、ProfilePoolBinding、QuotaObservation）都有 `to_dict() -> dict`。API 不接受 JSON text、Path、檔案／stream handle、env 或任意自訂 Mapping；解碼、來源 parsing、I/O 與 account authority 驗證是 caller 工作。payload 必須是 exact built-in dict/list/str/int/None（只在指定處可用 None）；bool 不算 int，float／Decimal object／tuple 等不算 wire 值。context operands 的 descriptors 是 0..16 個已由本 API 建立的 PoolDescriptor 的 exact tuple；unit_catalog 是 0..16 個 parsed UnitDefinition 的 exact tuple。兩者不可傳 raw dict、假 subclass／duck object，不讀 global state；需要該 keyword 的 API 必須明確提供，空輸入也寫 `()`。

每個 record root 必須有 `schema_version: 1`。本文件列的 keys 是全集，全部 required（variant 列出的額外 keys 只適用該 variant）；無任意 metadata／extensions、預設填值、alias key、型別 coercion 或 silently dropped key。錯誤未經 caller 明確改為 unknown 前，不能被 parser 自動修補成合法資料。

primitive 定義：

- `ID`：ASCII regex `[A-Za-z0-9][A-Za-z0-9._:-]{0,127}` 全字串匹配；大小寫敏感，不 trim／lower／Unicode normalize。ID 不得帶 credential；A 只驗 shape，不能證明 nonsecret／真實發行者。範例用 `fixture-account-a` 等合成值。
- `Reason`：ASCII `[a-z][a-z0-9-]{0,63}`，caller-controlled、非自由訊息；例如 `unresolved-alias`、`missing-source-time`、`external-host-unobserved`。未知 reason code 保留，不須改 core。
- `Ref`：portable `namespace:body`，namespace full match `[a-z][a-z0-9+.-]{1,31}`；body 為 1 個以上 ASCII 字元，只允許字母、數字與 `._~:/?#[]@!$&'()*+,;=%-`，整體最多 1024 bytes。例 `fixture:pool-authority/v1`、`https://example.invalid/contract#v1` 合法；空 body、空白、反斜線、control、非 ASCII、單字元 namespace 不合法。不 URL fetch、不 percent decode、不檢查 existence／hash 真實性，也不把合法 Ref 當簽章。
- `Time`：非 bool 整數 UTC epoch milliseconds，0..253402300799999 inclusive。不接受日期字串、秒單位猜測、float、naive datetime 或 implicit timezone conversion。
- `Duration`：非 bool 整數 milliseconds，1..31622400000 inclusive（366 天）。TTL／window duration 同 wire cap，不代表任何 provider reset／方案承諾。
- `K<T>`：known `{"state":"known","value":T}`，unknown `{"state":"unknown","reason":Reason}`；variant keys 嚴格且互斥。不以 null、空字串或 0 當 unknown。
- `UnitRef`：`{"unit_id":ID,"version":ID}`。同 UnitRef 的 definition 必須全等，沒有 implicit unit aliases。
- `PoolRef`：`{"authority_id":ID,"account_id":ID,"pool_id":ID,"revision":ID}`。這四欄共同 identify descriptor；account 不從 executor／model 推導。
- `ProfileRef`：`{"schema_version":1,"key":STRING}`，key 全匹配 `epk:v1:(request|resolved|observed|actual):[0-9a-f]{64}`。此為 #849 design 的 frozen framing fixture，不是 profile validation／hash verification；domain 原封不動，不把 request/resolved 升成 observed/actual。來源 pin 見 D9。

bounded validation：

1. 單一 payload root depth=1；dict/list value 進下一層，dict key 也算 parent depth+1；每個 container、key、scalar/null 都算一 node。最大 depth=16、nodes=4096，均 inclusive；shared object 每次出現按 traversal occurrence 重算，cycle 拒收，不能靠 alias 少算。
2. 每字串（含 key）先檢查長度不超 1024 code points，再驗合法 ASCII grammar；各 ID／Reason 等有更小上限。PoolDescriptor/Binding/Observation 最大 payload bytes=65536，standalone UnitDefinition 最大=2048；計 `J(payload)` 的 UTF-8 bytes：JSON object keys lexicographic sorted、無 whitespace、`ensure_ascii=False`、separator `,`/`:`、標準 JSON string escape；本 wire 合法字串都是 ASCII、數值只有 bounded int，沒有 encoder 的 float 歧義。不能先 unbounded deepcopy 或序列化才檢查 node/depth/string cap；UnitDefinition 同樣受 depth/node/string 上限約束。
3. parser 的 input 是已在 caller 記憶體中的 dict，不承諾限制 caller 原始 JSON transport／配置記憶體。walker 在讀取下一 node/depth／字串時 fail closed；JSON bytes 在已 bounded walk 後計量，超限拒收。這不是 raw log reader／sandbox。
4. PoolDescriptor 自身每筆 <=65536 bytes，standalone UnitDefinition 每筆 <=2048 bytes。parse_binding 的 payload+最多16 descriptors 上界仍為 17×65536=1114112；parse_observation 再加最多16 standalone definitions，總 semantic bytes 上界為 17×65536+16×2048=1146880。總量按每個 operand 的 `J(to_dict())` 加上 `J(payload)` 計，重複傳入物件仍每次計數，union 合併不抵扣；inline units 已計入 descriptor，不另重算 standalone schema envelope。不存在額外 unbounded catalog／registry lookup。inline units／windows 各 1..16；binding constraints 1..64；known group members 1..64；provenance_refs 1..16；coverage gaps 0..32。16 項 standalone cap 是每 call 的有限 native-unit vocabulary，不是全供應商或 global catalog 覆蓋承諾。
5. exact equality 的限界應接受（若其他 grammar 亦合法），limit+1 拒收；測 depth/node/bytes 必須建立有效但逼近限界的 wire fixture，若該 shape 的 schema 上限更早生效，也須另以 validator 的 bounded walker seam 證明這三個機械上限，不能以未知 keys 提早報錯冒充資源測試。

## D2 — UnitDefinition catalog、descriptor 與多 constraint binding

### D2.1 Standalone UnitDefinition 與明示 catalog

`parse_unit_definition(payload)` 解析獨立 root，exact keys：
`schema_version, unit_id, version, quantity_kind, semantics_ref`；schema_version 是非 bool 整數 1，其餘四欄的 grammar 與下方 PoolDescriptor.units element 相同。UnitDefinition 是 immutable data record，沒有 authority_id/account_id/pool_id/issuer/provenance 欄位，不要求 pool 或 live source 存在。semantics_ref 僅由 caller 明示原生 unit 的語意版本引用，不 fetch、不認證來源；可用 `fixture:native-token/v1`，不能因 Ref 合法就相信數值或發行者。

`parse_observation(..., descriptors=..., unit_catalog=...)` 的 unit_catalog 是 caller 明確傳入的 exact tuple，0..16 個由 parse_unit_definition 建立的 exact UnitDefinition；沒有省略預設、global catalog、env／registry lookup 或自動補 unit。parse_pool_descriptor 與 parse_binding 的 API 不需要 catalog，維持 descriptor inline units 自足；原 descriptor-only observation 路徑須明示 `unit_catalog=()`，不能省略參數或依賴隱含 catalog。

UnitRef identity 是 (unit_id,version) 的逐字、大小寫敏感 pair。definition equality 比較 schema_version=1 及 unit_id/version/quantity_kind/semantics_ref 全部欄位；semantics_ref 比字串，不跟隨 Ref 判斷內容等價。建 observation 的 bounded resolution table 時，同時檢查 unit_catalog 與所有傳入 descriptors 的 inline units（包括這筆 observation 沒引用的 definition）：

- 在 unit_catalog 內同 UnitRef 重複，無論 definition 是否相等，都拒收 `duplicate_reference`；單一 PoolDescriptor.units 內同樣如此。
- 跨不同 PoolDescriptor，或 standalone catalog 與 descriptor inline units，允許同 UnitRef **僅當 definition 全等**；這是共享原生 unit 的重複引用，不另造 ID。任一 quantity_kind／semantics_ref 衝突拒收 `unit_conflict`，不能選 first/last wins、以來源順序蓋掉或作 alias 猜測。
- PoolRef 重複仍依既有規則拒收 duplicate_reference；catalog 不可用來補缺失 PoolRef/window。known UnitRef 在 union table 不存在則 `unresolved_reference`，malformed known ref 按 shape/identifier 錯誤拒收，不能降為 unknown。
- 同 UnitRef 跨來源的相等判斷不做 unit conversion、不發行 account／pool，也不證明 provider truth。每 call 的最多 272 個 distinct UnitRefs（16 catalog + 16×16 inline）只是有限驗證索引，不是持久化 registry。

### D2.2 PoolDescriptor 與 binding

`PoolDescriptor` root keys：
`schema_version, authority_id, account_id, pool_id, revision, authority_ref, provenance_refs, units, windows`。

identity fields 是 ID；authority_ref 是 Ref；provenance_refs 是非重複 Ref list。authority_ref 只指向 caller 聲稱的發行／mapping 契約，不 fetch、不驗簽、不認證。未知 account／authority 不得捏造 descriptor：在 binding constraint 或 observation scope 使用 unknown，原來源由 provenance 表達。

每個 units element：
`{"unit_id":ID,"version":ID,"quantity_kind":"amount"|"gauge","semantics_ref":Ref}`。
inline units 保留原四個 keys，不新增 schema_version 或 catalog reference；外層 PoolDescriptor.schema_version=1 定義其 inline grammar。解析後可使用同一 immutable UnitDefinition 語意，但 PoolDescriptor.to_dict 必須仍輸出上述四欄，standalone UnitDefinition.to_dict 則輸出含 schema_version 的五欄。這不是忽略額外 key 或 implicit default；兩種 wire contexts 的 exact shape 明定不同。units 以 UnitRef 唯一；跨 descriptors 同 UnitRef 的相等／衝突規則與 D2.1 一致。token、request、premium-request、credit、time、api-equivalent-cost 等只作合成例，不是 closed provider/model allowlist；換算、價格、可付費餘額均非 A。gauge 是某時點併發名額等狀態，不是可累加用量。

每個 windows element 是下列 exact variant：

```text
fixed:         {window_id: ID, kind: "fixed", unit_ref: UnitRef, duration_ms: Duration}
rolling:       {window_id: ID, kind: "rolling", unit_ref: UnitRef, duration_ms: Duration}
instantaneous: {window_id: ID, kind: "instantaneous", unit_ref: UnitRef}
unknown:       {window_id: ID, kind: "unknown", unit_ref: UnitRef, reason: Reason}
```

window_id 在 descriptor 內唯一，unit_ref 必須指向該 descriptor 的 unit。amount 可用 fixed/rolling/unknown，gauge 只能 instantaneous/unknown；不能用 gauge 固定週窗再累加。unknown window kind 仍是一個未知限制，不能等於沒有該 constraint。

`ProfilePoolBinding` root keys：
`schema_version, binding_id, revision, subject, constraints, coverage`。

subject exact variants：

```text
profile: {kind: "profile", profile_ref: K<ProfileRef>}
group:   {kind: "group", group_ref: Ref, revision: ID, members: K<list[ProfileRef]>}
```

known members 不重複；group_ref 是 caller-provided group definition reference，不執行 alias expansion 或證明 members 完整／真實。新 model/new native effort 只改 upstream profile／caller members，不改此 core。

每個 constraint 為 `K<{"pool_ref":PoolRef,"window_id":ID}>`。known 值必須精確 resolve 到傳入 descriptor/window，找不到或 ambiguous descriptor（相同 PoolRef 多筆）拒收 `unresolved_reference`／`duplicate_reference`；不能 auto unknown。要保留 unresolved alias，caller 明確傳 unknown constraint。重複 known (PoolRef,window_id) 拒收；同一 pool 的 short/week 不重複、必須全部保留，不選一個當「代表額度」。

`binding_status` 回傳 immutable `{"state":"complete"|"incomplete","reasons":tuple[Reason,...]}`：
subject/member known、每個 constraint known 且 descriptor/window kind 已知、coverage.state=complete 才是 complete/reasons=()；其餘 incomplete，reason 集合 sorted、去重（`subject-unknown`、`constraint-unknown`、`window-unknown`、`coverage-incomplete`）。parse_binding 必須將所引用 descriptor/window 語意 snapshot 到不可變內部值，helper 不需要重找外部資料，也不借 caller mutation 改狀態。complete 只說聲稱的 references 完整；沒有 authority authenticity／可派工含義。

## D3 — Quantity wire

`DecimalWire` 是 canonical 非負十進位 ASCII string，full match：

```text
(?:0|[1-9][0-9]{0,35})(?:\.[0-9]{0,17}[1-9])?
```

最多 36 位整數、18 位小數；無正負號／exponent／whitespace／leading zero／trailing fractional zero。零唯一 `"0"`；`"0.33"`、`"1.000000000000000001"` 合法；`0.33`、`"-0"`、`"01"`、`"1.0"`、`"1e3"`、`"NaN"`、`"Infinity"` 非法。precision 是 wire cap，不偷偷 rounding；來源超出此精度的 B adapter 必須明示 unknown／另做版本化來源 mapping，不能被 A 截斷。

Amount exact variants：

```text
{kind: "exact", value: DecimalWire}
{kind: "bounds", lower: DecimalWire|null, upper: DecimalWire|null}
```

bounds 至少一側 non-null；有雙側時 lower<=upper，lower==upper 可保留 bounds kind；缺側意為沒有此側界，不是 infinity 或 zero，不推導 exact。Decimal 解析／比較必須 context-independent 且不經 float：可用整數係數+scale，或 Decimal 的精確字串建構與比較但不可受 ambient precision 的算術 rounding 影響。to_dict 保留 canonical wire 字串／kind。沒有 units conversion、balance subtraction 或會計加總 API。

Quantity exact variants：

```text
{state: "observed", amount: Amount}
{state: "estimated", amount: Amount, method_ref: Ref}
{state: "unknown", reason: Reason}
```

unknown 完全沒有 amount/method_ref；estimated 必有可定位估算方法的 Ref，但 A 不認證／執行該方法。known quantity 必須有已 resolve 的 UnitRef；unit unknown 不得數值為 observed/estimated。數值 0 是有效且已知的量，不是缺資料替代。不同 quantity kind 或不同 unit/version 永不在 A 相加、相減或排序成 admission verdict。

## D4 — Observation、source、coverage 與 reference consistency

`QuotaObservation` root keys：
`schema_version, observation_id, scope, profile_ref, unit_ref, window_instance, measurement, observed_at_ms, received_at_ms, ttl_ms, reset_at_ms, source, coverage`。

- observation_id 是 caller receipt ID；不等於 source event ID。scope 為 `K<{"pool_ref":PoolRef,"window_id":ID}>`；profile_ref 為 K<ProfileRef>；unit_ref 為 K<UnitRef>。
- known scope 必須精確 resolve 到傳入 descriptor/window；known unit_ref 必須 resolve 到 D2.1 的 explicit standalone+inline unit union。descriptors=() 仍可用 standalone UnitDefinition 保留 unknown scope 的 known 原生 usage，無須 account／pool／issuer；不能因此替 consumption 歸戶。known scope 且 unit_ref known 時，UnitRef 必須與該 window 完全相同（否則 incompatible_semantics），definition 亦須一致（同 Ref 衝突是 unit_conflict）；catalog 不能繞過或補造缺少的 scope/window。unit_ref unknown 不自動填值，只能配四種 quantity measurement 的 unknown quantity 或不含 quantity 的 limit_signal，精確分支見下文。
- observed_at_ms 與 reset_at_ms 為 K<Time>，received_at_ms 為 Time，ttl_ms 為 K<Duration>。若 observed/ttl 均 known，兩者相加不能超出 Time 最大值；違者 invalid_time。received 早於 observed 不由 parser 猜時鐘方向或改寫，交 D6 的 explicit-now 判斷。
- window_instance 的 exact variants：`{"kind":"interval","start_ms":Time,"end_ms":Time,"epoch":K<ID>}`；`{"kind":"instant","at_ms":Time}`；`{"kind":"unknown","reason":Reason}`。interval 要 start<end。已知 fixed/rolling window 的 known instance 必須是 interval 且 end-start=descriptor.duration_ms；instantaneous 必須 instant；unknown window kind 只允許 unknown instance。unknown scope 時可原樣保留合法 instance，不能聲稱完成 pool/window mapping。epoch 未知不捏造 window 重置序號，C 再處理 reset／epoch identity；A 不算 provider window anchor 或 snapshot coverage。
- unknown window_instance 允許保存部分證據；不是 unconstrained window。window_instance 限定觀測聲稱適用的 quota 窗，不是 log 讀取範圍或完整 consumption coverage。

measurement exact variants：

```text
{kind: "remaining_snapshot", metric_id: ID, quantity: Quantity}
{kind: "usage_delta", metric_id: ID, quantity: Quantity}
{kind: "usage_total", metric_id: ID, quantity: Quantity,
 counter: {scope_id: ID, epoch: K<ID>}}
{kind: "gauge_snapshot", metric_id: ID, quantity: Quantity}
{kind: "limit_signal", metric_id: ID, signal: ID}
```

quantity measurement 分兩個明確分支：(a) unit_ref known：先 resolve definition；quantity_kind=amount 只可 remaining_snapshot／usage_delta／usage_total，gauge 只可 gauge_snapshot，**即使 quantity unknown 也須遵守已知 unit kind**。(b) unit_ref unknown：remaining_snapshot／usage_delta／usage_total／gauge_snapshot 四種皆可保存，但 quantity 必須是 unknown；observed/estimated 數值一律 incompatible_semantics，不可自動推斷 unit 或升級 quantity。這兩分支都不取消獨立的 known scope/window 約束：若已知 scope 的 window 所引用的 UnitDefinition.quantity_kind 與 measurement kind 矛盾，仍 incompatible_semantics，但不把 unknown UnitRef 偷填 known。scope unknown、unit_ref unknown、quantity unknown 時四種都可接受，其他 required fields/variants 仍須合法。

limit_signal 不帶 quantity/counter，unit_ref 可 known（照常 resolve）或 unknown；既有 provider_status/structured_event method 限制不變，不由 rate-limit rejection 推導 0 剩餘。usage_total 的 counter.scope_id 與 epoch 保存累計語意，epoch unknown 仍可存證但不能自動差分；usage_delta/total 的差分、加總、重播消重或 consumption ledger 皆是 C。失敗 terminal 的來源 usage 是否有效由 B 的來源契約判定，A 不含 success-only filter，也不改既有 usage 記錄。

source exact keys：
`source_id, source_schema, adapter_version, authority_ref, method, provenance_refs, event_identity`。

source_id/source_schema/adapter_version 是 ID；authority_ref、provenance_refs 同 D2。method 是 `provider_status|structured_event|executor_usage|estimate|legacy`，這是取得方法的 semantic enum，不是 provider/product enum。event_identity 為 `{"state":"known","namespace":ID,"epoch":ID,"event_id":ID}` 或 `{"state":"unknown","reason":Reason}`；known tuple 必須由來源 namespace 的發行契約支持，不能是 collector 推算或 receipt ID，A 只驗形狀。

source.method=estimate 時有 quantity 的 measurement 必須 estimated 或 unknown；method=legacy 時必須 unknown。其餘三種 method 可 observed／estimated／unknown（例如從 structured source 推算的量仍是 estimated，method_ref 不能省）。limit_signal 只允許 provider_status／structured_event；其他 method 不可冒充直接 rejection/status evidence。authority_ref／provenance 非空也不證明官方、可信或已資格化。

Coverage 共用 exact shape：
`{"state":"complete"|"partial"|"unknown","gaps":[{"scope":ID,"reason":Reason},...]}`。
gaps 成對唯一、bounded；complete 必須 gaps=[]；partial 必須至少一項 gap；unknown 可空或列已知 gap。complete 僅 caller 對此次已宣告範圍的 assertion，A 不查外部 session／host、不推論全 account 覆蓋。unknown scope/subject 不會被 coverage complete 掩蓋；binding_status 仍 incomplete。consumer 不能以合法 coverage label 當 authorization。

### D4a — Cold-start 與 unknown-unit normative fixtures

以下是純合成 contract fixture，不是實作或 live evidence。先以 parse_unit_definition 解析下列 standalone payload，並以 `unit_catalog=(u,)` 明確注入；不建立任何 PoolDescriptor：

```json
{"schema_version":1,"unit_id":"fixture-native-token","version":"1","quantity_kind":"amount","semantics_ref":"fixture:native-token/v1"}
```

C1 observation payload：

```json
{
  "schema_version": 1,
  "observation_id": "fixture-receipt-1",
  "scope": {"state": "unknown", "reason": "unresolved-account"},
  "profile_ref": {"state": "unknown", "reason": "unresolved-profile"},
  "unit_ref": {"state": "known", "value": {"unit_id": "fixture-native-token", "version": "1"}},
  "window_instance": {"kind": "unknown", "reason": "unresolved-window"},
  "measurement": {"kind": "usage_delta", "metric_id": "fixture-token-usage", "quantity": {"state": "observed", "amount": {"kind": "exact", "value": "123"}}},
  "observed_at_ms": {"state": "known", "value": 1000},
  "received_at_ms": 1001,
  "ttl_ms": {"state": "known", "value": 60000},
  "reset_at_ms": {"state": "unknown", "reason": "missing-reset"},
  "source": {
    "source_id": "fixture-source", "source_schema": "fixture-event-v1", "adapter_version": "fixture-adapter-v1",
    "authority_ref": "fixture:source-contract/v1", "method": "executor_usage",
    "provenance_refs": ["fixture:source-event/123"],
    "event_identity": {"state": "unknown", "reason": "missing-event-id"}
  },
  "coverage": {"state": "unknown", "gaps": []}
}
```

C1：`parse_observation(payload, descriptors=(), unit_catalog=(u,))` MUST 接受，保留 observed exact `"123"` 和 unknown scope/profile/window/coverage；不能新增或猜 account、pool、issuer。source.authority_ref 仍僅來源契約的 caller assertion，與不存在的 account issuer 無關。event_identity helper 仍 unavailable。這保留未歸戶 native evidence，不聲稱 account remaining。

C2：由 C1 只將 unit_ref 改為 `{"state":"unknown","reason":"missing-unit"}`、quantity 改為 `{"state":"unknown","reason":"missing-unit"}`，用 `descriptors=(), unit_catalog=()`，MUST 接受。對 remaining_snapshot／usage_delta／usage_total／gauge_snapshot 四種 quantity measurement parameterize；usage_total 仍需 `counter={"scope_id":"fixture-counter","epoch":{"state":"unknown","reason":"missing-counter-epoch"}}`，其餘不得有 counter。不得因 unit unknown 而將四種全拒或硬猜 amount/gauge。

C3 單一故障 oracle（其他欄位必須合法，不能用多故障先後順序冒充精確診斷）：

| case | 預期 |
| --- | --- |
| C1 known UnitRef 不變，但 unit_catalog=() 且 descriptors=() | unresolved_reference；不把 123 改為 unknown 或捏造 pool |
| known UnitRef 多／缺 key，或 unit_id grammar 不合法 | invalid_shape／invalid_identifier；不 fail-soft |
| unit_catalog 傳 raw dict 而非 parsed UnitDefinition，或傳 list 而非 tuple | invalid_type |
| unit_catalog 同 UnitRef 重複（包含 byte-identical definitions） | duplicate_reference |
| 不同輸入來源同 UnitRef 的 quantity_kind 或 semantics_ref 衝突 | unit_conflict；不 first/last wins |
| 不同 PoolDescriptor/standalone 來源同 UnitRef 且 definition 全等 | 接受；原 descriptor-only 路徑配 unit_catalog=() 亦接受 |
| known scope 引用 missing PoolRef/window；或 known UnitRef 與該 window 不同而兩者皆可 resolve | unresolved_reference／incompatible_semantics |
| C2 quantity 改 observed 或 estimated numeric（後者有合法 method_ref） | incompatible_semantics；未知單位不能帶數值 |
| known amount + gauge_snapshot，或 known gauge + usage_delta（即使 quantity unknown） | incompatible_semantics |
| unit_ref unknown 且 quantity unknown，但 known scope 的 window 宣告與 measurement kind 矛盾 | incompatible_semantics；保留已知 window 約束，不暗填 UnitRef |
| catalog 17 項，或 standalone UnitDefinition J 超過 2048 bytes | resource_limit；schema 上限更早生效時另測 bounded walker seam |
| limit_signal 夾 quantity/counter，或 source method 是 estimate/legacy | invalid_shape／incompatible_semantics；既有 nonnumeric/method 限制不變 |

## D5 — Replay identity，不是 ledger key

`event_identity(obs)` 回傳 immutable available 或 unavailable：

```text
available:   {state: "available",
              key: ("qev:v1", source.source_id, namespace, epoch, event_id)}
unavailable: {state: "unavailable", reason: "source-event-id-unavailable"}
```

只有 source.event_identity 的 known variant 可 available；unknown、provider 無 event ID 或 adapter 沒有可信的來源 identity 契約時必須 unavailable。沒有 fallback hash、observation_id、received_at、profile key、window guess 或自增序號。A 不能驗 provider 是否真的發行該 ID；caller 在不知 namespace／epoch 語意時應傳 unknown。

此 key identify 一個來源事件，**不保證一 observation 一 key**：同事件可以帶 token、credit、兩個 window、不同 measurement fragments；相同 key 搭配衝突 payload 亦可被 A 各自接受並傳給 C。source_schema／adapter_version／receipt time 改變不改這五個 key components；換 namespace/epoch/source_id 的 alias migration、重複 collector、payload conflict、dedup key/fragments 和 durable watermark 由 C 判定。可用 key 不是「已消重」，更不是 exactly-once 或 account ID 證明。

## D6 — Pure freshness 與時序限界

`freshness` 的 now_utc_ms 必須是 Time；allowed_clock_skew_ms 必須是非 bool int 0..300000 inclusive，caller 明確給值（測試通常 0），不讀實際時鐘。回傳 immutable `{"state":"fresh"|"stale"|"unknown","reason":Reason}`，依以下 precedence：

1. observed_at_ms 或 ttl_ms unknown → unknown/`missing-freshness-input`。
2. observed_at_ms > now_utc_ms + allowed_clock_skew_ms → unknown/`future-source-time`（有界時鐘異常，不假 full）。
3. expiry = observed_at_ms + ttl_ms；若 reset_at_ms known，取兩者 minimum；若已知 window_instance 是 interval，再取 end_ms minimum。now>=expiry → stale/`expired-observation`，等號即 stale；否則 fresh/`within-ttl`。

所有時間 arithmetic 使用 exact integers；now+skew 用寬整數比較，不窄化 overflow。received_at_ms 不參與 expiry，不因重收重播延長壽命；reset 已過只能 stale，不能 refill 或產生新 observation。future time 在 skew 內容忍可回 fresh，但不使 unknown quantity 變已知；fresh 是時間分類，不是來源可信／足夠額度／可派工。

A 無先前 now／high-water state，**不能證明跨呼叫 clock rollback、event ordering、source conflict 或 crash recovery**。rolling interval 只保存 caller 提供的範圍，freshness 不滑動 window、不重算 usage；instant 與 fixed 的 provider 保證、缺 snapshot／external consumption coverage 由 B/C/D。clock 後退仍落在 TTL 內可能 fresh，是這個 stateless helper 的明示限制，不可寫成完整 rollback 防護。

## D7 — Deep immutability、錯誤與版本演進

record 內部為 frozen dataclasses + immutable tuples／scalar snapshots，所有 child dict/list 都深拷貝為不可變結構；frozen 外殼包可變 dict 不算達標。UnitDefinition、explicit catalog tuple、descriptor inline-unit snapshot 與 observation 已 resolve 的 unit definition 均不可變；不得對 caller input、unit_catalog、descriptors 或分享的 child container 做 in-place normalization。to_dict 每次產生新 built-in containers；改原 UnitDefinition payload、descriptor source、原 observation 或結果 dict/list 均不影響既有 records、binding_status、freshness 或 event key。衝突比較不依 mutable/global catalog。

UnitDefinition.to_dict 保留五欄 standalone root；PoolDescriptor.to_dict 保留四欄 inline units；Observation.to_dict 仍只有 D4 原 wire keys/UnitRef，不注入 catalog 或內部 resolution cache。重播 serialization 必須由 caller 再提供所需 explicit context：例如用 `parse_unit_definition(u.to_dict())` 重建 catalog，再 `parse_observation(o.to_dict(), descriptors=(), unit_catalog=(u2,))`；known ref 所需 context 省略/缺失會拒收，不聲稱 observation JSON 自帶 unit catalog 或 account mapping。各 root 的 J/to_dict 等價不含 cryptographic fingerprint；D1 J 只是穩定輸出 oracle，不複製 #849 key 算法。

`QuotaContractError(code, locator)` 的 code 全集：
`invalid_type, invalid_shape, unsupported_schema, invalid_identifier, resource_limit, cyclic_input, duplicate_reference, unresolved_reference, unit_conflict, invalid_decimal, invalid_bounds, invalid_time, incompatible_semantics`。
locator 僅已知 schema field 名與 bounded numeric index 組成的 tuple；unknown key 用固定 `"<unknown>"`，不得把未驗字串、Ref、path、source payload 或 credential 值放進 exception/log。若多個錯誤並存，不承諾哪個 code 先被報；單一故障負例才釘 exact code。不可吞錯轉 0／full／empty constraint。helpers 的 invalid args 同用 QuotaContractError。

上述 error vocabulary 適用於已進入 validation 的 wire/context 值。Python 呼叫本身缺 required positional/keyword argument 或帶不存在的 keyword，由明列 signature 以 TypeError 拒絕；例如省略 unit_catalog 與明示 unit_catalog=() 卻缺 known UnitRef 不同，後者是 unresolved_reference。這不准許 implicit context/default 或吞掉 wire error。

新增 provider/agent/model/native effort、ID、unit_id、Reason、Ref、source_schema／adapter_version 值都走既有 grammar，資料延伸不升 core version。R3 修補形成於尚未 accepted/frozen/released 的 v1 draft 階段；目前僅規劃內容 accepted，仍不是已發布 API 的相容性或 migration 證據；明定新 required unit_catalog 參數，舊 descriptor-only 資料 shape 配 `unit_catalog=()` 仍有完整路徑。正式 v1 發布後變更 core keys／variant semantic enum／Decimal grammar／limit／profile-ref framing／event key projection 或 freshness 語意須新 core schema version 與 migration/conformance 設計，不偷偷擴 v1。某 adapter 新 protocol 版本仍只是 source_schema／adapter_version 新值；不可僅因 adapter protocol change 就升 core schema。#849 未產品時只用 frozen fixture；正式 upstream 出現後由獨立 conformance 工作判斷相容，不 import 假 API 或自己重寫 profile key。

## D8 — Test oracle、scope 與 sizing

預定 test file `tests/test_quota_observation.py`：表格 parameterized tests + bounded property cases；全部 synthetic，不讀真 provider logs、credential、registry、installed state 或觸發模型。每 I1–I10 各有可獨立失敗的反例；限界使用 exact-bound／bound+1，不能只跑 happy path。

必要對照：D4a C1 cold-start descriptors=()／standalone catalog 保留 native "123"；C2 空兩 context 的四種 unknown-unit/unknown-quantity；C3 malformed/missing known ref、各來源內 duplicate、跨來源相等／衝突、window mismatch、numeric-without-unit、known amount/gauge mismatch 和 catalog caps；同 PoolRef 不同 profile 共用；不同 account 不混同；short/week／unknown alias；0 vs unknown；decimal precision/bounds；observed/estimated/legacy；future/TTL/reset/end 等號；receipt 改變 key 不變；同 event 多 fragment 不消重；UnitDefinition/context/to_dict deep mutation／cycle/resource cap；request key 不升 actual；上游 fixture 不當 qualification。

真 production scope 只新增純 quota contract；standalone UnitDefinition 是同一 schema 的原生量綱資料型別與 caller-input reference resolution，不是新 account issuer、global unit registry、provider adapter 或 conversion domain。因此 domain=0 的 owner/writer/decision 範圍未擴，並非因仍是一個檔案而壓分。state=1 源於 versioned compatibility 和局部 reference consistency，**零 durable transition**；catalog 每 call 有限驗證、不持久化或跨呼叫 merge。R3 增加 API/型別、collision/cap/unknown 分支測試工作，沒有藉不加分省掉它們；新 ledger state 仍不在這一分內。invariant_count=10 仍對應 I1–I10，UnitDefinition 風險由 I2/I4/I5/I6 的既有 contract 覆蓋，不代表 envelope 合格。

真 helper 採 pin 588d7d8 + 既有 fix-standard：9 cards、9 persona bindings、2 core gates，固定適用 R-09/R-16/R-19；acceptance=2、orchestration=2。draft 與 memory-only accepted 分開實跑，預期分別 5/Yellow completeness=false、7/Red completeness=true；後者不修改文件或 authority。#831 後 5/Yellow 只投影，未在此更換 control。需實際有 current envelope 才能講能力；lookup=None 的 observable bypass 不是新 model／native effort 資格。現場任何 quota exhausted 都不能被此 planning check 蓋過。

## D9 — Intake、upstream 與殘餘界線

此 design／spec／todo 與 own proposal／design／tasks 六個 view 同一 work_item、同 0/1/10、同 source/tests/documentation、同 R-09/R-16/R-19；不維護兩套需求。child owner #866／parent #836、R3 獨立 planning review PASS 與 root 規劃內容 acceptance 已完成；exact publication、發布版 strict、正式唯一 mapping／source binding／frozen authority 仍待 root 完成 intake。accepted 不表示已有 PR／發布／dispatch／qualification，不許據此派工。

正式 freeze 後六 views 的 operator baseline 不改；candidate 僅容許依現有 authority tolerance 且有證據的 checkbox toggles，任何非 checkbox 的內容／scope/hash 修改先回 root 正式 authority 流程。新本地或 review 證據各寫新 receipt，不覆寫歷史／immutable report 或舊核收。

T11 pre-archive 只要求 local focused/full tests、與 CI exact engine/manifest steps 對齊的 local preflight/policy、diff、local build/wheel install smoke，以及本地 collection 加既有 CI argv 核對。PR 未存在時明示 intended title/body/labels/base/head；這不是 remote CI 或真 PR 已通過。T12 只核對 candidate coverage ledger／可驗本地證據，逐項保留尚未發生的正式 review/remote CI pending，不要求 builder 先替 Manager 完成 independent review。正式 Manager independent exact-candidate review 與 finding 處置仍必需，可依 workflow 時序在 archive 前發生，但不是 T12 的先決 checkbox。own archive／其後 reverify、remote Python 3.10–3.13 CI/build/install smoke、真 PR-context gate、PR／merge、installed/live／母 issue closure 仍各自需要正式證據；產品 Tasks 全可在 own archive 前閉合，不能以勾完 local Tasks 代替這些下游核收。

Pin：`588d7d8bd3dbfe9e758a6ac1750f1bff2ba45e86`。[#849 frozen profile framing](https://github.com/hamanpaul/paulsha-cortex/blob/588d7d8bd3dbfe9e758a6ac1750f1bff2ba45e86/docs/superpowers/specs/execution-profile-schema-core-design.md) 只供 fixtures，正式 upstream conformance pending。現有 [registry.update_headless_result](https://github.com/hamanpaul/paulsha-cortex/blob/588d7d8bd3dbfe9e758a6ac1750f1bff2ba45e86/paulsha_cortex/coordinator/registry.py#L1333) → [extract_usage](https://github.com/hamanpaul/paulsha-cortex/blob/588d7d8bd3dbfe9e758a6ac1750f1bff2ba45e86/paulsha_cortex/coordinator/usage_extractors.py#L205) 和 [StreamEvidence](https://github.com/hamanpaul/paulsha-cortex/blob/588d7d8bd3dbfe9e758a6ac1750f1bff2ba45e86/paulsha_cortex/coordinator/outcome_taxonomy.py#L391) 是 B 的既有 reuse seam，A 不另寫 parser。workflow usage aggregate、model_profile 舊 fingerprint、model_identities／model_resolution 不是 quota authority。

Residuals（有界列管、不宣稱關閉）：A 信任 caller 對 ID 非機敏／發行者／coverage 的陳述，不能驗真；missing official state 仍 unknown；source ID 可用也沒有 dedup；stateless freshness 無歷史 rollback 防護；#849 consumer 尚未存在；B/C/D 未接線；全 provider 覆蓋算法與 live issuer／account binding deployment 未完成。各殘餘被 public API 無授權判斷與零 runtime consumer 限制；後續若要把 A 結果餵入 admission，必須另行完整 source/auth/coverage/ledger gate，不能把此殘餘接受擴成 live 許可。

## Open Questions

無未決的 A wire／語意問題。上述工程選擇已具限界、原因與負例；尚未完成的 intake／正式 upstream conformance／live authority 是明列依賴與權限，不以「無 Open Questions」假稱它們完成。
