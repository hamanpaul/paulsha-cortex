---
type: added
scope: coordinator
---
新增 `paulsha_cortex/coordinator/quota_observation.py` 的 T2 純 stdlib quota observation
contract core：bounded walker、immutable records、strict standalone unit parsing、trusted
tuple-only descriptor/unit catalog context boundary、parser-sealed DTO constructor
boundary、canonical `UnitDefinition.to_dict()`（descriptor inline unit wire shape 維持
原樣）、redacted `QuotaContractError`，以及僅支援 current RED fixtures 的 standalone
unit-catalog observation parsing。另補強 T2 已公開 parser scaffold 的 strict enum／shape
boundary：descriptor window kind、measurement variant keys、`source.method` 與
`coverage.state` 現在都會拒收未列舉或錯形 wire；不接 consumer、不做 I/O、也不讀
global catalog。另把尚未真正實作的 descriptor-backed observation path 改為
fail-closed：known `scope` 直接回 `unresolved_reference`、descriptor window 的
`unit_ref` 必須指向同一 descriptor 內的 inline unit、`window_instance.kind`
只接受 `interval|instant|unknown` 與各自 exact keys；已知 standalone `unit_ref`
也會對 known amount/gauge measurement kind mismatch fail-closed，已知 binding
constraint 則必須精確對到唯一 supplied descriptor window。另修正 `PoolDescriptor`、`ProfilePoolBinding` 與
`QuotaObservation` 的 public record equality/hash semantics，現在會反映完整 wire payload，
避免僅因公開欄位子集相同而把不同結構誤判成相等。
