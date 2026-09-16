---
type: fix
scope: coordinator
---
**Issue #814：`retry-build --reason` 的 operator 裁決以明示指令進 builder prompt，並回 operator 注入收據**

#752／#757 已把裁決逐字放進 contract 的 `operator_adjudications` 鍵，但 prompt 沒有任何一句話告訴
builder 那是 operator 的權威裁決、必須實作——retry_context 有明示語句、裁決沒有；實機 builder 因此把它
當 metadata 略過，交出 tree 與前候選相同的空 commit，而 verifier 讀同一份 evidence 逐條判 failed，裁決
要繞 reviewer findings 轉述一輪（約 40 分鐘）才到得了 builder。本次補上與 #606 retry_context 同型的固定
語句 `OPERATOR_ADJUDICATION_DIRECTIVE`（權威、優先於模型 findings、完成前必須實作、verifier 讀同一份、
空 commit 會被打回），無裁決時整句缺席、prompt 逐字不變；`retry-build`／`retry-card` 回傳新增
`adjudication.next_step_hint`，明示裁決已記錄且會於下一次該卡 dispatch 注入。維持 #757 的 run 級語意
（裁決隨每次派工出現，不做「消費後不再注入」），evidence 不可變、不動 retry_context 與 reviewer independence。
