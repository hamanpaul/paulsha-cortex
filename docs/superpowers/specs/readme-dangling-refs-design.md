---
status: accepted
work_item: readme-dangling-refs
---

# 清除 README 懸空引用設計

## Decisions

### D1 行內標記，不用 allowlist

R-22 的 `.doc-drift-allow` 是以「文件路徑」或「symbol 名稱」為單位放行。用文件路徑放行等於整份 README 不再檢查，會把以後新造成的懸空引用也一起藏起來。所以 runtime 檔名改用行內的 `<!-- doc-drift-ignore -->`，只影響那一行。

### D2 repo 內檔案一律寫完整路徑

讀者看到 `manager_daemon.py` 不知道它在哪裡，R-22 也找不到。寫成完整路徑同時解決兩個問題，而且之後檔案搬家時 R-22 會報出來。

### D3 同一行同時有兩類引用

如果同一行既有 repo 檔案、又有 runtime 檔名：先把 repo 檔案改成完整路徑。行內標記會讓整行都不檢查，所以這種行要拆成兩行，或者確認改寫後的完整路徑確實存在，再加標記，並在 terminal reason 說明。

### D4 驗收用完整清單

R-22 的輸出只顯示前 20 筆，README 剛好排在最前面。修完之後，前 20 筆會換成 `docs/**` 的項目，看起來像「README 已經沒有了」，但不能證明。所以驗收看的是總數變化：候選 head 比候選 base 少了 base 上 README 的筆數，而且輸出裡沒有 `README.md ->`。進件本身新增的文件也可能帶來新的 advisory 項目，所以基準要用候選的 base，不能用固定數字。

### D5 Sizing

純文件，一個檔案，不碰 production code 與 state，`domain_breadth: 0`、`state_consistency: 0`。
