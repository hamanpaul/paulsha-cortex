# Execution profile schema／key core

## Scope and status

`paulsha_cortex.coordinator.execution_profile` 是 Cortex #849 的 pure-data
schema/key core。它只接受 caller 明示交入的 mapping、JSON text 或 bytes，使用
Python standard library 驗證與序列化 descriptor、profile 與 requirements；不讀
檔案、環境、clock、catalog／registry，不啟動 subprocess／network，也不自行選擇
model、provider、role 或 effort。

本文件描述已交付的 core API 與契約邊界。它不宣稱既有 production routing、
qualification、state migration、installed artifact 或 live launcher 已接線。

## Public API

```python
from paulsha_cortex.coordinator.execution_profile import (
    actual_condition_key,
    actual_condition_missing_fields,
    canonical_profile_bytes,
    parse_descriptor,
    parse_profile,
    profile_key,
)
```

- `parse_descriptor(value)` 解析一個版本化 adapter/model descriptor。
- `parse_profile(value, descriptor)` 解析一筆單一 plane 的 profile record；同一
  descriptor 可分別解析 `requested`、`resolved`、`observed`，但三筆資料不會互相
  覆蓋。
- `canonical_profile_bytes(profile)` 回傳 record projection 的 D4 typed canonical
  bytes，不含 key framing。
- `profile_key(profile)` 依 record plane 使用 `request`、`resolved` 或 `observed`
  domain；caller 不能把一個 plane 改名成另一個 domain。
- `actual_condition_key(profile)` 只接受 observed 且所有必要 conditions 都是
  known（effort 可合法 `not_applicable`）的 record；否則回傳 `None`。
- `actual_condition_missing_fields(profile)` 回傳排序後的缺失 locator，方便上游
  誠實呈現 unknown 原因。

回傳的 descriptor/profile 是 parser-sealed immutable value。輸入 mapping/list 不會
保留 alias，`to_dict()` 每次都回傳可修改的獨立 snapshot；error 只帶 machine code
與 field locator，不回印任意原始值。

## Version, effort and plane rules

v1 的 descriptor 與 profile 都必須明示 `schema_version: 1`。缺版本、`bool` 版本、
未知未來版、缺欄位與額外欄位都 fail-closed；core 不把 legacy payload silent
migrate 成 v1，也不回寫既有 state。

descriptor 以資料描述 adapter、model 與 `effort_grammar`。effort grammar 可是
string、integer、finite binary64 number，或由 object／array 組成的巢狀 grammar；
object 的 required 欄位與 array 順序由 descriptor 明示。新增 model、原生 effort 值，
或既有 adapter 的 `protocol_id`、`protocol_version`、`runtime_version` 新值，都是
descriptor-only 擴充：core wire version 維持 v1，條件改變時 actual key 自然改變。
這些值不是中央 resolver 的產品名單，也不建立跨 adapter 的同名 effort 等價。

profile 的 `requested`、`resolved`、`observed` 是三個獨立 plane。每個 tagged value
只能是：

- `{"state":"known","value": ...}`：保留明示的原生型別；integer、float、string、
  `+0.0`／`-0.0` 不混同。
- `{"state":"unknown","reason":"..."}`：保留未知原因，不猜填 value。
- `{"state":"not_applicable"}`：只在 descriptor 的頂層 effort grammar 是 `none`
  時可用。

requirements 的 `role`、`minimum_quality`、`pin`、`independence` 是資料欄位，不是
批准 API。合法 schema、完整 actual key 或 fake provenance 都不等於
approved／qualified／permission grant；真實 eligibility、role、pin、independence
與真人 receipt 由下游 owner 決定。

## Canonical bytes and keys

canonicalization 依 D4 固定下列語意：

- projection 保留 tagged wrapper；record projection 是
  `schema_version`、`plane`、`conditions`、`requirements`，actual projection 是
  `schema_version` 與完整 `conditions`。
- `toolset`／`permissions` 是集合，依 typed bytes 的 unsigned UTF-8 順序排序並移除
  byte-identical 重複；其他 array 保留輸入順序。
- typed tree 使用固定 node tags：null `n`、boolean `b`、integer `i`、finite float
  `f`、string `s`、array `a`、object `o`。object member 依未 escaped key 的 UTF-8
  bytes 排序。
- renderer 只輸出 UTF-8、無 whitespace／BOM／尾端 newline；U+0000–U+001F 一律用
  lowercase `\u00xx`，不使用 `\n`／`\t` 短 escape，不 escape `/` 或非 ASCII。
- key frame 是
  `ASCII("cortex.execution-profile") || NUL || ASCII("v1") || NUL ||`
  `ASCII(domain) || NUL || uint64_be(len(C)) || C`，再取 SHA-256；完整外部格式為
  `epk:v1:<domain>:<64-hex-digest>`。

actual-condition key 只反映完整 observed execution conditions。adapter／protocol／
runtime version、model/revision、effort、loadout、toolset、sandbox、permissions 或
toolchain 改變都必須可辨識；requirements、provenance、descriptor 清單、pricing、
時間戳、外部 report／approval receipt reference 不進 actual key。排除 provenance
不表示 proof 已驗證，仍須由 #842 或其他授權 owner 驗證。

core wire／projection／canonical encoding 改變才是 schema/key version 升版；只有
adapter protocol 欄位換成新的 descriptor value 不升 core version。這兩件事不可混為
一談，也不會覆寫舊 key 或 receipt。

## D5 bounded validation

v1 parser 的 semantic bounds 是：

| 資源 | 上限 | 語意 |
|---|---:|---|
| nesting depth | 16 | root 計為 depth 1；超一層即 `depth_exceeded` |
| semantic nodes | 4096 | object key 另算 string node；超出即 `node_count_exceeded` |
| semantic typed bytes | 65536 | descriptor＋profile 依 D4 typed bytes 合計；超出即 `semantic_size_exceeded` |
| JSON text operand | 1048576 UTF-8 bytes | descriptor/profile 各自獨立；超出即 `transport_too_large` |

semantic bytes 不是 serializer 排版長度；它由完整 descriptor/profile 的 typed data
計算，包含 metadata、provenance 與 grammar，且 shared native object 每次出現都計數。
native mapping 沒有 raw-text guard，但同樣受 semantic depth/node/byte bounds 約束。
解析 JSON text 時先做有界 transport／lexical 檢查，不能先無界 materialize 再判定超界。

在 transport 上限內，compact／pretty／等價 escaping 的 JSON text 與 native mapping
只要代表同一 typed data，必須得到相同 semantic verdict、canonical bytes 與 key。超大
padding 即使不改變 semantic data，也可以先被獨立 transport guard 拒絕；這是
`transport_too_large`，不是新 schema/key 不相容，也不能用 serializer 排版來繞過它。

## Ownership and parent acceptance criteria

| owner | 本 core 的邊界 | 不由本 core 宣稱完成 |
|---|---|---|
| #849 | descriptor/profile/requirements schema、immutable values、D4 canonical bytes／keys、D5 bounded validation 與 fixture tests | production routing、真 observed producer、qualification、migration |
| #835 | schema/key 的母件 adapter conformance、production consumer 與相容 binding | 本文件不代替母件尚未交付的接線與 live evidence |
| #842 | report → candidate → human-review receipt → approved roster、撤銷／有效期／CAS、legacy qualification migration | core key 或測試 pass 不等於 approval |
| #581 | doctor planning identities、unknown-role、非 dispatch provenance、doctor overlay API 與其 eval producer 管線 | core 不查 runtime role eligibility |
| PatchMUD #37 | 外部 CLI/file report、profile-aware fingerprint 與 immutable fixture/revision | Cortex 不 import PatchMUD、不代修 producer、不用本 core 產 benchmark |

母件的 resolver／launcher／workflow／registry routing、quota／forecast／reservation／
admission、installed package、real launcher 與 live acceptance 都是獨立交付。未知或
legacy qualification 沒有可信 authority 時維持 unknown／legacy，不由本 core 補值。

## Delivery accounting

| 項目 | 本 child 狀態 |
|---|---|
| core implementation | 已在 `paulsha_cortex/coordinator/execution_profile.py` 交付 |
| core tests | 已在 `tests/test_execution_profile_schema_core.py` 及既有回歸套件驗證 |
| public package import | fresh-process fixture 從 checkout 外以 public API 驗證；這不是 installed/live proof |
| PR merge | 尚待 Manager 的 delivery／merge gate；本 child 不宣稱已 merge |
| parent routing／qualification／migration | 不在本 child；尚未以本文件宣稱完成 |
| installed／live | 不在本 child；需各自授權與 receipt |

本件沒有新增 CLI flag、subcommand、model／agent／effort 固定清單或 runtime probe。
既有 `--help` smoke 維持原命令契約；core API 以 module import 使用，不需要修改
package `__init__` 或現有 CLI。
