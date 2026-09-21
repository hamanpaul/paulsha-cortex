---
type: added
scope: coordinator
---
新增 `paulsha_cortex/coordinator/quota_observation.py`，提供純 stdlib 的 quota observation
contract core：bounded walker、immutable records、strict standalone unit parsing、顯式
descriptor/unit catalog context 的 observation 解析，以及 redacted `QuotaContractError`
失敗邊界；不接 consumer、不做 I/O、也不讀 global catalog。
