# #831 stability-risk-v2

- 修正 planning sizing 的 `spec_stability` 風險方向，保留其他 sizing 維度與 Green／Yellow／Red 門檻。
- 完整 accepted 三件組為 0、單一缺失 kind 為 1、至少兩個缺失 kind 或 blocker／未 accepted artifact 為 2；unknown 或不一致 report 保守為 2。
- 舊 run、frozen planning、CompletionRecord 與 immutable evidence 不會被讀取或重啟流程回填；沒有算法來源的歷史分數維持 legacy／unversioned。
