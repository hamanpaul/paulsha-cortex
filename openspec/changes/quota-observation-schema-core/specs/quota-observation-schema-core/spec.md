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

# Quota observation schema core delta

此 capability 對應 owner [#866](https://github.com/hamanpaul/paulsha-cortex/issues/866)、parent #836 的 `quota-observation-schema-core`。R3 fresh independent planning review PASS 後，root 已接受規劃內容；proposal/design/tasks 已 upfront 備齊，尚未 PR／發布／formal frozen authority／dispatch／qualification，亦非產品／live 通過。精確 wire 以同 change 的 design D1–D9 為準，十個 requirements 與 spec I1–I10 等價。

## ADDED Requirements

### Requirement: I1 Pure data contract without runtime integration
The quota observation core MUST 僅提供 in-memory validation、immutable records、explicit-time freshness 與 source-event identity，不 I/O、不讀 credential、不新增 existing production caller 或 dispatch side effect。

#### Scenario: Parse synthetic input without touching live state
- **WHEN** caller 以 synthetic payload 呼叫 public API，且 open/env/subprocess/network 被 sentinel 禁用
- **THEN** 合法資料可解析且原輸入不變，沒有 registry／consumer／模型／服務操作

### Requirement: I2 Strict bounded immutable wire
The quota observation core MUST 依 design D1/D7 驗 exact keys/types/version、depth/node/byte/array caps，拒收 cycle，保持 deep immutable snapshot，並用不回顯 payload 的 stable error code。

#### Scenario: Reject malformed and over-limit records
- **WHEN** single-fault fixture 有未知 key/version、bool-as-int、cycle 或超過任一 inclusive cap
- **THEN** 回 QuotaContractError 對應 code，不吞成 unknown／0，錯誤不包含原 payload/ref/path

#### Scenario: Preserve record after caller mutation
- **WHEN** caller 修改原 payload、descriptor source 或 to_dict 回傳值的深層 list/dict
- **THEN** 已解析 record、binding_status 與 event key 不變

### Requirement: I3 Versioned opaque profile reference without qualification
The quota observation core MUST 只保存 frozen versioned profile-ref framing 與原 key domain，不複製 #849 fingerprint／descriptor，不 import 尚不存在的 upstream API；新 agent/model/native effort MUST 可經 caller ref 延伸。

#### Scenario: Legal fake key remains only a reference
- **WHEN** request/resolved/observed/actual 的 synthetic key 滿足 v1 framing
- **THEN** parser 保留 domain 和字串，但不證 hash 真實、不升 actual、也不產生模型資格

### Requirement: I4 Explicit shared account pool and binding
The quota observation core MUST 接受 caller 提供的 opaque authority/account/pool IDs 與 refs，只驗 shape/reference consistency；未知 alias/group/scope MUST 顯式 incomplete/unknown，不發行或猜測 ID。

#### Scenario: Share a pool without splitting by model
- **WHEN** 不同 profile 引用同 PoolRef，另有不同 account 的 PoolRef
- **THEN** 前者保持共享、後者保持不同，不能從 executor/model 或 credential hash 推導 mapping

#### Scenario: Distinguish unknown from broken known reference
- **WHEN** caller 明示 unknown constraint，或另給已知但不存在的 descriptor/window reference
- **THEN** 前者可記錄但 binding incomplete，後者以 unresolved_reference 拒收而不自動修補

### Requirement: I5 Preserve simultaneous windows and gauge semantics
The quota observation core MUST 保留每個 pool/window constraint，不合併 short/week，不把 unknown window 當 unconstrained，並依 D2/D4 區分 fixed/rolling/instantaneous、amount/gauge。

#### Scenario: Multiple constraints are not interchangeable
- **WHEN** 同 pool 包含 short 與 week window，或 gauge 被聲稱成 usage_delta
- **THEN** 前者兩者都保存，後者 incompatible_semantics；重複 known constraint 拒收

### Requirement: I6 Exact native quantity without implicit conversion
The quota observation core MUST 使用 D3 bounded canonical Decimal string，分開 UnitRef/version，支援同純 module 的 standalone immutable UnitDefinition 及 caller explicit bounded unit_catalog，無需 account/pool/issuer。MUST 拒收 malformed/missing known refs、來源內 duplicates、跨來源 definition conflicts、numeric without unit 與已知 amount/gauge/window 矛盾；unknown unit MUST 可配四種 quantity measurement 的 unknown quantity，不做 conversion／加總、global lookup 或 admission ranking。

#### Scenario: Cold-start native usage without any known account
- **WHEN** D4a C1 使用 descriptors=()、一筆 parse_unit_definition 建立的 native amount unit 與 explicit unit_catalog，observation 的 scope/profile/window unknown、usage_delta observed exact "123"
- **THEN** 接受並保留 "123"，不製造 account/pool/issuer；UnitDefinition 的 semantics_ref 只驗 shape，不當來源信任或資格

#### Scenario: Unknown unit and unknown quantity retain all four measurement kinds
- **WHEN** D4a C2 使用 descriptors=()/unit_catalog=()，scope/unit/quantity unknown，parameterize remaining_snapshot/usage_delta/usage_total/gauge_snapshot 且其餘 variant 欄位合法
- **THEN** 四種皆接受；observed/estimated numeric 則拒收，known unit 或 known window 存在時仍執行其 amount/gauge 一致性，不從 unknown 偷填 UnitRef

#### Scenario: Explicit catalog equality and reference failures
- **WHEN** 同 catalog 重複 UnitRef、跨 catalog/descriptor 同 Ref 定義衝突、known ref 缺失/不合法或 known scope/window 不匹配
- **THEN** 依 D2.1/D4a 的 duplicate_reference/unit_conflict/unresolved_reference/invalid_shape/invalid_identifier/incompatible_semantics 拒收；跨不同來源同 Ref 全等則接受，unit_catalog=() 的原 descriptor-only path 保留

#### Scenario: Catalog bounds immutability and contextual serialization
- **WHEN** caller 使用 standalone root 五欄、inline root 四欄、catalog 0..16、D1 bytes/cross-input caps，或修改原 context/to_dict 結果
- **THEN** bounded deep snapshots 不變且 serializations 保留各自 exact shape；roundtrip 必須重傳 explicit context，超限／缺 known context 拒收，不讀 global state

#### Scenario: Fractional and unknown are not truncated or conflated
- **WHEN** quantity 是合法 fractional request/credit、已知零或 unknown
- **THEN** 精確保存其狀態和值；unknown 不變零／無限，token 不自動換 credit

#### Scenario: Reject invalid precision and bounds
- **WHEN** 輸入負值、float、NaN/Inf、exponent、非 canonical decimal、超精度、空雙側或 lower>upper
- **THEN** 以 invalid_type、invalid_decimal 或 invalid_bounds 的 single-fault oracle 拒收，不截斷或修正

### Requirement: I7 Source and coverage do not promote unknown evidence
The quota observation core MUST 依 D3/D4 分開 observed/estimated/unknown、source method、provenance 與 coverage；合法 Ref 或 complete label MUST NOT 證明真實來源或全 account coverage。

#### Scenario: Estimates and legacy retain evidence class
- **WHEN** source.method=estimate 卻聲稱 observed、legacy 卻有 known quantity，或 limit_signal 帶 numeric quantity
- **THEN** 拒收，不將來源缺失或 rejection 轉 remaining=0

#### Scenario: Explicit coverage gaps remain visible
- **WHEN** 外部 host/session coverage 未知或 partial
- **THEN** 保存 state/gaps，不自動升 complete 或授權可派工

### Requirement: I8 Stateless freshness never replenishes quota
The quota observation core MUST 依 D6 caller now/skew 和 observed_at+TTL、known reset/window end 的 minimum 作 pure freshness，received_at MUST NOT 延長 observation 壽命。

#### Scenario: Expiry and reset equality are stale
- **WHEN** now 等於或超過 expiry/reset/window end
- **THEN** 回 stale，不 full reset、不改 quantity 或產新 observation

#### Scenario: Missing or future time is not infinite freshness
- **WHEN** observed/TTL unknown 或 source time 超過 now+skew
- **THEN** 回 unknown；core 不宣稱具跨呼叫 rollback／restart 防護

### Requirement: I9 Source-event identity is not deduplication
The quota observation core MUST 只在 source-issued namespace/epoch/event ID 已知時回 D5 available key，缺少時回 unavailable；MUST NOT 以 receipt/hash/time 補 ID 或執行 ledger dedup/reconciliation。

#### Scenario: Replayed receipts and multiple fragments
- **WHEN** 同來源事件重收而 receipt／adapter version 改變，或一事件產生多 measurement
- **THEN** event key 相同且各 observation 可保留；衝突／去重／fragment identity 交 C，不宣稱 exactly-once

#### Scenario: Provider does not expose event identity
- **WHEN** source.event_identity 是 unknown
- **THEN** 回 source-event-id-unavailable，不發明唯一 key

### Requirement: I10 Schema success grants no operational authority
The quota observation core MUST NOT 產生 authorized/eligible/can_dispatch/reservation/qualification 判斷；planning checks、完整 binding、freshness 與 event key MUST 與產品／live 驗收分帳。交付 MUST 區分 pre-archive local evidence 與正式 Manager independent review／remote CI／PR 下游 gates；freeze 後六 views 的 operator baseline MUST 保持不變，candidate 只容許有證據且符合 authority tolerance 的 checkbox toggles，非 checkbox 修改須先回 root 正式 authority 流程，新證據 MUST 另出 receipt 而不覆寫 immutable history。

#### Scenario: Valid but unauthenticated input
- **WHEN** caller 填入 shape 合法 authority/account IDs、complete coverage 和 fresh time
- **THEN** 只可得到結構／時間／event identity 結果，不能認證 ID、提升模型資格或派工

#### Scenario: Local task closure does not require a fabricated formal review
- **WHEN** builder 完成本地 focused/full、CI-exact pinned local preflight、diff/wheel smoke、test collection 與 coverage ledger，而真 PR／remote CI 或正式 Manager review 尚未發生
- **THEN** 只按本地證據核對 pre-archive Tasks，intended PR context 明列 intended；review/remote CI 仍 pending 且必需，不要求 builder 先冒簽 review，不修改 frozen 非 checkbox 內容或舊 receipt

#### Scenario: Child delivery does not close parent obligations
- **WHEN** A schema 與 synthetic fixture tests 完成但 #849 conformance、B/C/D 與 live account/source gate 尚未完成
- **THEN** 母 #836、forecast/reservation/admission 與 deployed/live claims 仍維持各自未完成狀態
