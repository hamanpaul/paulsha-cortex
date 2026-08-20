# 755-retry-build-reason

- **`#755` `--reason` 擴到 `retry-build`——operator 對 repair 回合的指示終於有
  通道。** 實機：code-review 散文 passed 但兩條 minor findings 落在 blocking
  category（correctness）→ gate 依 #617 的 category 判準 rejected；此後
  `retry-card` 拒絕已綁 evidence 的卡、`retry-build` 又沒有 `--reason`——builder
  只看得到 stale 的跨卡回饋（#750 取非通過 terminal，散文 passed 的那顆不在集合），
  於是採用原 HEAD 不動、review replay 再拒，確定性迴圈。修法：抽共用
  `_record_operator_adjudication`／`_validate_operator_adjudication_args`
  （retry-card 收斂到同一支），`retry-build` 增列選填 `reason` 落
  `cortex-operator-adjudication/v1` evidence（card=subagent-build）。#750 的
  rejection 來源涵蓋 gate-evaluation-rejected 形狀與 #617 本體另票處理。
