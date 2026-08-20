# 757-adjudication-scope

- **`#757` operator 裁決改為 run 級獨立 prompt 區塊——不再因 candidate 換新而
  消失。** verify/review 卡的 retry-context matching 以 candidate 定錨，repair 後
  candidate 換新即空；#752 把裁決掛在 retry_context 底下，於是 verification-37 的
  prompt 完全沒有裁決、reviewer 重新升級已裁決的 D3 矛盾。修法：
  `_workflow_job_prompt` 增獨立 `operator_adjudications` 參數與 contract 鍵——
  evidence 存在即隨每一次派工出現（builder／reviewer 皆然）；無裁決時鍵缺席、
  prompt 逐字不變。retry_context 的舊掛點保留相容。
