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
global catalog。
