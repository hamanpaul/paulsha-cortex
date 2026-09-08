---
status: accepted
work_item: execution-profile-schema-core
---

# Execution profile schema／key core 設計

## Scope

依 [Spec](execution-profile-schema-core-spec.md) C01–C10，唯一 production 檔為新
`paulsha_cortex/coordinator/execution_profile.py`。以下 API／型別是待 Cortex 實作的設計，尚不存在。
不在 `model_identities`／`model_resolution`／`launcher`／`planning_runtime`／`workflow`／`registry`
加 import 或轉換 shim；正常 `from ...execution_profile import ...` 不需要改 package exports。

## Decisions

### D1 — 公開純 API 與資料責任

建議同一模組提供 `parse_descriptor`、`parse_profile`、`canonical_profile_bytes`、`profile_key`、
`actual_condition_key` 與 immutable value 的 `to_dict`；解析接受 caller 提供的 mapping 或 JSON text，
不接受檔案路徑／環境名稱／runtime object。JSON decoder 用 duplicate-key hook，數值非有限先拒絕。
`parse_profile` 每次只處理一個 plane，並接受對應 descriptor；caller 可分別持有三筆紀錄。
observed 若屬不同 model／adapter，保留其對應 descriptor 下的真值，不拿 requested descriptor 逼它改名；
無對應 descriptor 的資料保留在上游原 report，本 core 不假裝驗過或補成原請求。

v1 record 頂層恰為六個必填欄位：`schema_version`、`plane`、`conditions`、`requirements`、
`provenance`、`metadata`；沒有隱含預設或額外頂層欄位。`schema_version` 是非 bool 的整數 1，
plane 僅 `requested`／`resolved`／`observed`；record 不含 approved／qualified。
requirements 恰有四個必填鍵 `role`、`minimum_quality`、`pin`、`independence`，值均使用 D2 的
known／unknown wrapper，不能使用 not_applicable。known role 的 value 是非空 ID string 或 null；
其餘三者的 known value 是有界 JSON data，core 不解釋內部政策 vocabulary。明示 known null 表示
caller 沒加該項額外限制，與 unknown 不同；兩者都不能解除下游原有最低品質／Trust Root gate。
core 不把未知角色映射為 build、不查 runtime 是否認得該角色；語意／eligibility 仍由 #581 與母件
resolver 擁有。requirements 內的 array 保序，不由本 core 猜測哪一種政策集合可重排。

`provenance` 為有序 array，每筆恰有非空 ID string `kind` 與非空 portable reference string `ref`；
無來源時明示 `[]`，外部 proof 真偽交 producer／#842 驗證。ref 的 v1 grammar 恰為
`namespace ":" body`：namespace 匹配 `[a-z][a-z0-9+.-]{1,31}`，body 至少一字元，只能使用
ASCII letters/digits 與 `._~:/?#[]@!$&'()*+,;=%-`；整筆上限 1024 ASCII bytes，沒有 trim、percent
decode 或 URI normalization。這是可攜的 opaque identifier 語法，不是 URL scheme 白名單／網址有效性
或 credential scanner；未知 namespace 合法但不表示 proof 可取得或可信，也不會 dereference／開檔。
`urn:fixture:observation-7`、`artifact:reports/r7.json`、`https://example.invalid/r/7` 是合法字面值；
空字串、`urn:`、`/tmp/report.json`、`C:\temp\r.json`、`urn:one two`、前後空白／control／非 ASCII
字元不合法。含憑證或無法跨環境解析的 reference 仍由 producer／#842 政策拒絕，不假裝語法檢查已完成該責任。
`metadata` 是 object，頂層可選鍵只有
`pricing`、`pricing_provenance`、`timestamps`、`evidence_refs`、`approval_receipt_refs`、`discovery`，
值為有界 JSON data；無 metadata 時明示 `{}`。這些 opaque data 內部允許 caller 的資料鍵，不建立
定價／政策 vocabulary。它們與 requirements 的 opaque values 是明示例外，不能據此放寬其他 strict object。

### D2 — 原生 effort 的有界描述語法

descriptor 頂層恰有必填 `schema_version`（integer 1）、`id`（非空 ID string）、`adapter`、`model`、
`effort_grammar`、`provenance`、`metadata`。adapter/model 是 D3 對應 value 的完整 object；若 profile
相應 condition 為 known，須逐型別／值相符；unknown 不猜填。其餘執行條件由 D3 固定資料語法描述，
本件不藉 descriptor 做 launcher conformance。provenance/metadata 沿用 D1 語法且不進 key。

effort grammar node 為 strict object，必填 `type`。以下是全部合法 node shape；方括號內表示可選鍵，
不是 JSON 值。未列鍵一律拒絕，不支援 `$ref`、executable 或擅自轉型。

| type | 其他合法鍵與 value 判準 |
|---|---|
| `none` | 無其他鍵，只能作頂層 effort grammar；只接受 not_applicable wrapper |
| `string` | 可選 `enum`：非空、無重複的 string array；未提供 enum 時接受任意合法 Unicode string |
| `integer` | 可選 `min`、`max`：同型別 integer，min≤max；value 必須是 integer，bool/float 不可代用 |
| `number` | 可選 `min`、`max`：finite binary64 float，min≤max；value 必須是 float，integer/bool 不可代用 |
| `object` | 必填 `properties`（欄位名→grammar object）、`required`（無重複的已宣告欄位名 array）；拒絕額外 value property |
| `array` | 必填 `items` grammar，value 為有序 array |
| `boolean`、`null` | 無其他鍵，只能是 object/array 的後代 node，接受對應原生型別 |

這是資料型別語法，不是全域 effort 值或產品清單。新增模型與 grammar 內的原生值只改 descriptor。

每個 tagged value 的完整 wire shape 只能是 `{"state":"known","value":...}`、
`{"state":"unknown","reason":"..."}` 或 `{"state":"not_applicable"}`，不得有其他鍵。
known 必須有符合 grammar 的 value；unknown reason 是非空、無頭尾空白的 string，不能帶 value；
effort 的 not_applicable 只在 grammar=none 時允許。known string 的字面值 `"unknown"` 不是 unknown
狀態；empty／0／null 也不得被 truthiness 轉成 missing。bool 不算 integer/number。
core 不選 default：未指定的 request 以 unknown/reason 保留，resolved default 由上游明示 value/provenance。

### D3 — 執行條件與 key 排除表

`conditions` 恰有以下八個必填鍵，每個值使用 D2 tagged wrapper；只有 effort 可用 not_applicable。
下表是 known wrapper 內 `value` 的完整 shape；表內 object 的鍵均必填且不可增加。
ID string 與版本／revision 均為非空、無頭尾空白的 Unicode string，大小寫逐 byte 保存，不列產品表。

| condition key | known value 的完整 shape |
|---|---|
| `adapter` | `{"id":ID,"protocol_id":ID,"protocol_version":ID,"runtime_version":ID}` |
| `model` | `{"id":ID,"revision":ID}` |
| `effort` | 精確符合該 descriptor effort_grammar 的 value |
| `loadout` | `{"id":ID,"version":ID}` |
| `toolset` | `[{"id":ID,"version":ID}, ...]`，空集合 `[]` 合法 |
| `sandbox` | `{"id":ID,"version":ID}`，是契約 reference，不是本機路徑 |
| `permissions` | `[{"id":ID,"version":ID}, ...]`，空集合 `[]` 合法 |
| `toolchain` | `{"id":ID,"version":ID}` |

只把 known toolset/permissions 的 value 視為集合：逐元素依 D4 `J(T(element))` 的 unsigned byte
lexicographic order 排序，再移除 byte-identical 重複項。相同 id、不同 version 是不同元素，不猜等價。
其他 array 一律保序。known object 若缺子欄位直接拒絕；部分未知需由 producer 明示整個 condition
unknown 並保留原因／原 report，不自動將缺子欄位變成空字串或 known。
observed 的每個條件均 known，或 effort 為合法 not_applicable，才可構造 actual-condition key；
任一 unknown 回 `key=None` 與排序後的 missing-field locators。core 不驗 provider 是否真的使用此條件。

| 欄位 | record key | actual-condition key |
|---|---|---|
| schema/key 版本、plane domain、conditions（含明示 unknown） | 納入 | 只接受完整 observed conditions，使用獨立 actual domain |
| requirements | 納入，避免混淆請求意圖 | 排除；role/deck/coverage 與批准由 #842 組合資格 key |
| provenance reference／descriptor 清單與 discovery metadata | 排除，保留於 record 原資料 | 排除，不讓清單擴充使相同實際條件失效 |
| metadata 的 pricing／pricing provenance／時間戳／外部 report、approval receipt reference | 排除 | 排除 |

排除 provenance 的 hash 敏感度不代表信任該 provenance；#842 仍須驗 proof、role/coverage 與核可。
adapter/version 等實際條件不能藏在 metadata 以逃過變更識別，strict conditions 欄位必須完整存在。

### D4 — 唯一 normative v1 encoding

本節定義完整 wire→projection→typed tree→bytes→framed hash，不接受語意看似相同的其他 encoding。
例如 `["integer","1"]` 與 `{"type":"integer","value":"1"}` 都不是本契約的 integer node。

#### D4.1 — JSON text 與 native 型別

輸入 JSON text 必須是無 BOM 的合法 Unicode／UTF-8 JSON；number token 只接受
`-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?`。無 `.`／`e`／`E` 的 token 解析為任意精度
integer，不能先轉 double；`-0` 解析成 integer 0。帶小數點或 exponent 的 token 解析為 IEEE-754
binary64，依十進位精確值做 round-to-nearest, ties-to-even；包括 subnormal 與 underflow 至帶符號
zero，負號保留。因此 `1.0`、`1e0` 是同一 float，`1` 是不同 integer；`-0.0`／`-0e0` 是 negative
float zero。overflow 成 infinity 一律拒絕；NaN／Infinity token 本來就不是合法 JSON。
跨語言實作者須保留 number token 的上述型別資訊；會先將全部數字混成 double 的預設 parser 不符合契約。

native mapping 路徑只接受 exact JSON-compatible 型別：任意精度 integer、binary64 float、bool、null、
Unicode string、array、string-key object；bool 不屬於 integer，Decimal／其他物件不偷偷轉型。
serializer 的 float JSON token 必須含 `.` 或 exponent 且重解析後 binary64 bits 完全相同，包含 -0.0；
integer 使用無前置零十進位 token，不走浮點中介。serializer JSON 外觀不必唯一，以下 canonical bytes 必須唯一。

所有 object 在 JSON key escape 解碼後檢查重複，包括 `"a"`／`"\u0061"`；不得先用 last-key-wins。
字串允許合法 surrogate pair 解成單一 Unicode scalar，但 lone／倒置 surrogate 或非 UTF-8 一律拒絕。
不做 NFC／大小寫正規化。native 非有限 float、循環、非 string key 與 D5 越界資料同樣拒絕。

#### D4.2 — 完整 projection 與 node shape

令 `N` 為驗證後只按 D3 正規化兩個集合的 conditions；保留所有 tagged wrapper，不拆掉 state/value。
record 的 projection **恰為** `{"schema_version":1,"plane":P,"conditions":N,"requirements":R}`，
P 與 R 是該紀錄的 plane／完整 requirements。actual 的 projection **恰為**
`{"schema_version":1,"conditions":N}`，且只允許 observed／全 known（effort 可合法 not_applicable）。
若不符合 actual 前提，回 key=None／原因，不得 hash 不完整 projection。沒有其他預設欄位、descriptor
digest、provenance 或 metadata 進 projection；不得任意 unwrap、改平面或將 requirement 塞入 actual。

typed transform `T` 的 node 全為 JSON array，tag 與 arity 固定如下；表內 `...` 僅表示重複元素，
不是輸出文字。object member name `k` 是 bare JSON string，不再套 `["s",k]`。

| native value | 唯一 `T(value)` |
|---|---|
| null | `["n"]` |
| boolean b | `["b",b]`，b 是 JSON literal true／false，不是字串 |
| integer i | `["i",d]`，d 為無前置零十進位 string；zero 恰為 `"0"`，負數只有一個前導 `-` |
| finite float f | `["f",h]`，h 為 binary64 big-endian bits 的恰 16 個 lowercase hex digits |
| string s | `["s",s]` |
| array a | `["a",[T(a0),T(a1),...]]`；空 array 恰為 `["a",[]]` |
| object o | `["o",[[k0,T(v0)],[k1,T(v1)],...]]`，按未 escape 的 k 的 UTF-8 unsigned bytes 升序；空 object 為 `["o",[]]` |

#### D4.3 — JSON bytes、framing 與公開 key

`J` 是 typed tree 的唯一 JSON byte renderer：UTF-8，無 BOM、whitespace 或尾端 newline；array
只用 `[`、`]` 與元素間的 `,`。string 用 `"` 包圍，U+0022 只寫 `\"`、U+005C 只寫 `\\`；
U+0000–U+001F 一律寫六個 ASCII bytes 的 `\u00xx`（lowercase hex），**不用** `\n`／`\t` 等短寫。
其餘 Unicode scalar 直接 UTF-8，不 escape `/`、非 ASCII 或 U+2028/U+2029。boolean 只寫
ASCII `true`／`false`。T 輸出只包含 array、string、boolean，不再有 JSON number/object/null literal。

`C = J(T(projection))`；`canonical_profile_bytes` 回 C，不含下列 frame。record domain 明確映射
requested→`request`、resolved→`resolved`、observed→`observed`；actual domain 恰為 `actual`。
`profile_key` 只從 record plane 選 domain，不能讓 caller 把 requested 指定成 observed。
hash preimage `F` 的 bytes **恰為**以下串接，`00` 是一個 NUL byte，不是四字元 escape：

```text
ASCII("cortex.execution-profile") || 00 || ASCII("v1") || 00 ||
ASCII(domain) || 00 || uint64_be(len(C)) || C
```

`len(C)` 是 UTF-8 byte 數，不是字元數，長度欄是恰 8 bytes unsigned big-endian，沒有十進位文字、
冒號或 newline。digest = lowercase hex SHA-256(F)，對外完整 key 是 ASCII
`epk:v1:` + domain + `:` + digest（64 hex）。沒有終端 NUL 或 newline。改 node tag、projection、
framing 或版本均不相容，不能只改 key 外部 domain 字串而沿用 digest。

#### D4.4 — 完整 profile golden vector

以下是完整有效的合成 observed profile，無真實 observation／批准含意；對應 descriptor 的 adapter/model
與此 value 相同、id=`fixture-descriptor`、schema_version=1、effort_grammar=`{"type":"number"}`、
provenance=`[]`、metadata=`{}`。profile 中 metadata 的 pricing 故意存在但不進兩種 projection。

```json
{"schema_version":1,"plane":"observed","conditions":{"adapter":{"state":"known","value":{"id":"fixture-adapter","protocol_id":"fixture-wire","protocol_version":"1","runtime_version":"1"}},"model":{"state":"known","value":{"id":"fixture-model","revision":"r1"}},"effort":{"state":"known","value":1.0},"loadout":{"state":"known","value":{"id":"fixture-loadout","version":"1"}},"toolset":{"state":"known","value":[]},"sandbox":{"state":"known","value":{"id":"fixture-sandbox","version":"1"}},"permissions":{"state":"known","value":[]},"toolchain":{"state":"known","value":{"id":"fixture-toolchain","version":"1"}}},"requirements":{"role":{"state":"known","value":"review"},"minimum_quality":{"state":"known","value":null},"pin":{"state":"known","value":null},"independence":{"state":"known","value":null}},"provenance":[],"metadata":{"pricing":{"amount":9.0,"unit":"fixture"}}}
```

actual 的 C 恰為下一個 code block 的單一行 UTF-8 bytes，不含 Markdown fence／最後換行；長度 **920**：

```json
["o",[["conditions",["o",[["adapter",["o",[["state",["s","known"]],["value",["o",[["id",["s","fixture-adapter"]],["protocol_id",["s","fixture-wire"]],["protocol_version",["s","1"]],["runtime_version",["s","1"]]]]]]]],["effort",["o",[["state",["s","known"]],["value",["f","3ff0000000000000"]]]]],["loadout",["o",[["state",["s","known"]],["value",["o",[["id",["s","fixture-loadout"]],["version",["s","1"]]]]]]]],["model",["o",[["state",["s","known"]],["value",["o",[["id",["s","fixture-model"]],["revision",["s","r1"]]]]]]]],["permissions",["o",[["state",["s","known"]],["value",["a",[]]]]]],["sandbox",["o",[["state",["s","known"]],["value",["o",[["id",["s","fixture-sandbox"]],["version",["s","1"]]]]]]]],["toolchain",["o",[["state",["s","known"]],["value",["o",[["id",["s","fixture-toolchain"]],["version",["s","1"]]]]]]]],["toolset",["o",[["state",["s","known"]],["value",["a",[]]]]]]]]],["schema_version",["i","1"]]]]
```

actual frame 的前 43 bytes hex（包含長度欄）恰為
`636f727465782e657865637574696f6e2d70726f66696c650076310061637475616c000000000000000398`；
完整 F 長度 **963**，expected key：
`epk:v1:actual:cccfc64a59aed4d4f3c14caa2468b18ff1ea067eeb84e3f376e0e722799e9ac2`。

同一 profile 的 observed record C 恰為下列單一行，長度 **1227**：

```json
["o",[["conditions",["o",[["adapter",["o",[["state",["s","known"]],["value",["o",[["id",["s","fixture-adapter"]],["protocol_id",["s","fixture-wire"]],["protocol_version",["s","1"]],["runtime_version",["s","1"]]]]]]]],["effort",["o",[["state",["s","known"]],["value",["f","3ff0000000000000"]]]]],["loadout",["o",[["state",["s","known"]],["value",["o",[["id",["s","fixture-loadout"]],["version",["s","1"]]]]]]]],["model",["o",[["state",["s","known"]],["value",["o",[["id",["s","fixture-model"]],["revision",["s","r1"]]]]]]]],["permissions",["o",[["state",["s","known"]],["value",["a",[]]]]]],["sandbox",["o",[["state",["s","known"]],["value",["o",[["id",["s","fixture-sandbox"]],["version",["s","1"]]]]]]]],["toolchain",["o",[["state",["s","known"]],["value",["o",[["id",["s","fixture-toolchain"]],["version",["s","1"]]]]]]]],["toolset",["o",[["state",["s","known"]],["value",["a",[]]]]]]]]],["plane",["s","observed"]],["requirements",["o",[["independence",["o",[["state",["s","known"]],["value",["n"]]]]],["minimum_quality",["o",[["state",["s","known"]],["value",["n"]]]]],["pin",["o",[["state",["s","known"]],["value",["n"]]]]],["role",["o",[["state",["s","known"]],["value",["s","review"]]]]]]]],["schema_version",["i","1"]]]]
```

observed frame 的前 45 bytes hex 恰為
`636f727465782e657865637574696f6e2d70726f66696c65007631006f627365727665640000000000000004cb`；
完整 F 長度 **1272**，expected key：
`epk:v1:observed:e2750040b4f91c563760daa19bcaa808f66bab7f4c445c3765522f19609b841b`。

以下每列只把上述 profile 的 effort value 改成該 JSON token；descriptor type 相應改成 integer／number／
string，使每列仍是合法 profile，而不是用錯 descriptor 跳過驗證。五把完整 actual key 必須兩兩不同：

| effort JSON token | T(effort value) | expected 完整 actual key |
|---|---|---|
| `1` | `["i","1"]` | `epk:v1:actual:a5b73772473ec7f2926cc56d0117cf8b5e6ffa68431090d5be3fc552b91e729f` |
| `1.0` | `["f","3ff0000000000000"]` | `epk:v1:actual:cccfc64a59aed4d4f3c14caa2468b18ff1ea067eeb84e3f376e0e722799e9ac2` |
| `"1"` | `["s","1"]` | `epk:v1:actual:0bbfb460e7bd4e09122ac9737d14f9efb3ecba7387a562078a0496ce395a9e30` |
| `0.0` | `["f","0000000000000000"]` | `epk:v1:actual:987c15ceb122f1e723247646330890502786b63a53a13bf01311b7e06a53cbd6` |
| `-0.0` | `["f","8000000000000000"]` | `epk:v1:actual:a637e42ea4416af2af0e34e869c48ef3682dceec152f7124b0855d5550731f26` |

`1e0` 是 1.0 同值正例；`-0` 是 integer 0、不是 -0.0；JSON `+0.0` 語法不合法。
另需負控制：用其他 node shape、漏掉 length framing、只 hash C、保留 metadata、
將 known wrapper 拆掉或把 record requirements 留在 actual，均不得等於此 golden。
另外 `J(T({"z":"\n","a":"é/"}))` 恰為 UTF-8 的
`["o",[["a",["s","é/"]],["z",["s","\u000a"]]]]`（無尾端換行）；改用短 `\n` escape、
escape 非 ASCII／斜線、反轉 object member 次序都不符合這筆額外的字串 vector。
這些 expected bytes／keys 由文件外的記憶體腳本與獨立 SHA-256 工具交叉驗算，不是待實作產品的自產 expected。

### D5 — Strict、immutable 與資源界線

v1 固定 `MAX_DEPTH=16`、`MAX_NODES=4096`、`MAX_SEMANTIC_BYTES=65536`（64 KiB），等於上限
合法、超出一單位即拒絕。semantic metric 不使用 D4 serializer 的可變 JSON 外觀，規則如下：

- root 的 depth=1；object 的每個 key string／value、array 的每個 element，其 depth 都是父 container+1。
  primitive 與 container 都算 node；每個 object member key 另算一個 string node，不能漏算 key。
  空 object／array 各為一 node；例如 `{}` 是 depth=1/nodes=1，`{"a":0}` 是 depth=2/nodes=3。
- `parse_descriptor` 單獨計完整 descriptor；`parse_profile` 計完整 descriptor **加**一筆 profile：
  兩個 root 都各算一 node、各從 depth=1 開始；合計 depth 取兩者 maximum，nodes 取兩者 sum，
  不新增虛構 pair root。先前已解析的 immutable descriptor 也須計入，不得因呼叫順序不同免計。
- semantic bytes 恰為 `len(J(T(descriptor))) + len(J(T(profile)))`；單獨 parse_descriptor 只有第一項。
  使用 D4 的 UTF-8 typed bytes，但取完整資料，不取 key projection，不含 framing／分隔字元；
  metadata／provenance／grammar 都計入。計算在 D3 集合去重之前，不先刪重複元素來省額度。
  D4.4 的完整 golden profile 加該節描述的 descriptor，恰為 depth=5、nodes=144、semantic bytes=1740。
  native mapping 與 transport 界內解碼後同資料使用相同 metric，mapping 排序、JSON whitespace、
  等價 escaping 不改 semantic 裁決。不同型別的 1/1.0 不是同資料；重複集合元素是額外輸入資料，
  即使之後得到同一 key，也不代表輸入資源數相同。T 展開產生的 tags 不另算 depth/nodes。
- shared native object 每次出現都按值計數，不能用 identity memo 免計；目前 ancestry stack 再遇
  同一 container 即報循環。必須在 push／遞迴／複製下一 node 前檢查 depth/nodes。
  以有界累計器計 semantic byte 長度，超界立即停止，不先建整個 canonical tree／巨大字串副本。
  大型 native string／integer 可先以長度／位數安全下界拒絕，再做有限編碼；不保證任意 input 常數成本。

另有**獨立 transport guard**：每個 caller 提供的 JSON text operand 上限 `MAX_TEXT_BYTES=1048576`
（1 MiB UTF-8；等於合法），descriptor/profile 各自適用，不是兩者合併的 semantic budget。
core 不讀 stream／檔案；已有 native mapping 無 text guard。JSON text 在完整 decode／allocation 前，
先做有上限的 UTF-8 length／lexical 掃描；不得先無界 encode 整個字串或先 json.loads 再檢查深度。
掃描需理解 JSON string／escape，串流累計 depth/nodes 與 decoded token 的 semantic 長度；達到
任何上限後的第一個單位即中止，不完整 materialize 超界 value。file/stream adapter 日後由其 owner
以同一每 operand 上限做 bounded read，只能多讀一 byte 判定超界；本件不新增 I/O 入口。

錯誤碼分開：raw text 超界 `transport_too_large`；semantic byte 超界 `semantic_size_exceeded`；
深度／nodes 超界 `depth_exceeded`／`node_count_exceeded`；循環 `cyclic_value`。locator 可指出
descriptor/profile，不能回印原始值。transport 上限內的等價排版才保證同 semantic 裁決；極端 padding
可能先被 transport guard 拒絕，這不是新 schema/key 不相容。此有限讀取取捨已由 root 明確接受。
上列數值是版本化 parser 邊界，不是模型 quota；調整需契約重審，不偷偷用機器資源決定。

邊界 fixtures 必含：depth 16/17（root 計入）；nodes 合計 4096/4097；完整 typed bytes 合計
65536/65537；每份 raw text 1048576/1048577。還須同資料的 compact／pretty／escaped/native 路徑
在 transport 界內同裁決、單份都未超界但 descriptor+profile 合計超界、預解析 descriptor 仍計入、
超大 padding 早停、深層 native chain 在第 17 層前止步與共享 alias 重複計數；不得只在完整 decode 後測錯誤。
只接受明定的 JSON-compatible 型別；unknown fields、duplicate JSON keys、bool version、錯 union、
malformed descriptor、NaN/Inf、不可編碼字串都 fail-closed。

解析結果遞迴 immutable，不保留 caller 可修改的 dict/list alias；`to_dict` 回獨立副本。
不快取依賴外部 state 的值，不引入 file lock／CAS／migration；跨 process round-trip 只驗同一新資料。
metadata 頂層沒有 raw credentials／本機路徑欄位，error 不 echo arbitrary value；這不是通用機密偵測器，
caller／producer 仍負責送入可分享且不含秘密的資料。

### D6 — 合法升級與 downstream 接縫

v1 parser 的缺版本／未知未來版一律拒收，不猜成當下或某個舊版。合法擴充是在既有 descriptor grammar
內新增 model/effort 值，也包括 adapter.protocol_id/protocol_version/runtime_version 等已定義欄位的
新值：這些只更新 descriptor／對應 profile，沿用 core v1，actual key 自然改變；不需中央產品名單分支。
此處需要升版的 protocol/shape 專指 **core 資料 wire grammar／projection／canonical encoding** 改變，
不是 adapter protocol 欄位新值；前者才須明示新 core schema/key version、保留舊 key 契約，不覆寫 receipt。
legacy manifest／run 轉換、可信 snapshot 缺失的 legacy/unversioned/unknown 與 authority 重新接受，
由母 T05/T10 後續 child 負責；core 不為此讀取或升級既有 state。

#842 可在本 core 契約凍結後以明示 fixture 開發 publication consumer，無需等整件 #835 關閉；
其 human-review receipt／qualification lifecycle 不回灌成 core 的 approve API。#581 五項與
PatchMUD #37 CLI/file producer 各由原 owner 實作；新 module 不 import PatchMUD。
母件 adapter／resolver／binding child 日後接正式 producer/consumer 與 actual routing，
在此之前只能宣稱本 core 的純 API 可獨立測，不能宣稱 fallback、effort forwarding 或角色授權已生效。

## Validation

新增 `tests/test_execution_profile_schema_core.py`（C01/C02/C06/C07/C08/C10）、
`tests/test_execution_profile_keys.py`（C03/C04/C05）、
`tests/test_execution_profile_core_boundary.py`（C09/跨 process／import boundary）作為未來落點，
可在相同 scope 內合併測試檔；不可把新增檔名當既有測試成果。
fixture 一律虛構 adapter/model；string、integer、0.625、nested effort 分別驗 native 型別。
正例與越界／錯型別／unknown／未核可／future-version 負例成對；元測試刪掉關鍵欄位驗證時必變紅。
fixture consumer 只讀 core 公開 API，不能假扮 production resolver／真人資格批准。

既有 `tests/test_model_identities.py`、`tests/test_model_identities_envelope_v3.py`、
`tests/test_model_resolution_chain_534.py`、`tests/test_per_work_model_chain.py`、
`tests/test_model_profile_cli.py` 與 full suite 作 no-regression gate；這些舊 consumer 不因測試綠燈就算接線。
CLI 只做既有 --help smoke／文件說明，本件不加 flags、subcommands 或 native provider probe。
