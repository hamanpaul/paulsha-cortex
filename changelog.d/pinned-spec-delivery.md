---
type: fix
scope: coordinator
---
**Issue #503：slice-lane builder prompt 逐字交付 pinned spec，並由 Cortex attest 實際交付的 hash**

舊 `build_dispatch_prompt` 只給 `[TASK] <slice_id>` 與 `[PLAN: path]`，spec body（含 controller 重釘後補進的
recovery 指示）在模型邊界被靜默丟掉，registry 卻顯示新 spec hash。本次：dispatch 在登記 slice row 後重讀
spec，sha256 必須等於 pin 值（不等／不可讀／非 UTF-8 → 該 slice needs_human、不派 job）；prompt 附
`[SPEC: path sha256=…]`、固定明示語句（spec 是唯一 authority、與 plan 衝突以 spec 為準）與逐字 `[SPEC BODY]`
（上限 24k 字元，超過截斷並指示讀檔），builder 角色缺 spec 即拒絕組 prompt、非 builder 舊形狀不變；job row
新記錄實際交付的 `spec_hash`／`plan_hash`；完成側新增 `_builder_input_attestation_mismatches`，builder job
記錄的 hash 與 slice 釘住的不等即 `pinned-input-mismatch`（legacy job 缺欄位不判）；foreign review prompt
也附同一份 `[SPEC …]` 行。測試 fixture 新增 `tests/_pinned_spec_support.py`，把舊的假路徑／假 hash 落成真檔案。
