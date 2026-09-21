---
type: added
scope: coordinator
---
新增 `paulsha_cortex/coordinator/quota_observation.py` 的 T2 純 stdlib quota observation
contract core：bounded walker、immutable records、strict standalone unit parsing、trusted
tuple-only descriptor/unit catalog context boundary、parser-sealed DTO constructor
boundary、canonical `UnitDefinition.to_dict()`（descriptor inline unit wire shape 維持
原樣）、redacted `QuotaContractError`，以及僅支援 current RED fixtures 的 standalone
unit-catalog observation parsing；不接 consumer、不做 I/O、也不讀 global catalog。
