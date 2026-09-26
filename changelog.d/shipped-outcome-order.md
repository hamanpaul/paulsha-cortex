#1086：一般 review→ship 在 Manager 終態寫入前 durable 保存唯一 shipped outcome，並確認讀回；相同 Candidate、merge 與 CompletionRecord 的重入沿用既有 row，寫入、讀回或綁定衝突時不標 done。
