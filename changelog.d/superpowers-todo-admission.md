# 1054 build 前 Todo admission planning

- 修正 #1054 accepted packet，將 Manager first-Builder gate 限定為消費可信 source/freshness authority；補上 Todo/source digest 對 exact run claim 的檢查、stale direct resume fail-closed 與 pre-Builder abandon→new start 邊界。
- 查明 Monitor scanner/path link 與 WorkAuthority freshness 尚不滿足原 AC，建立硬依賴 #1063、#1064、#1065；本 Draft PR 僅更新規劃，不含產品程式碼或正式 intake。
