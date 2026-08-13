---
status: accepted
work_item: fix-read-repo-tier-fail-closed
---

# fix-read-repo-tier-fail-closed Todo

`#492`：canonical `.project-policy.yml` 存在但缺 `tier` 時，`read_repo_tier()` 對 `None` 直接拒絕，
而 deck/readiness/preflight 沒有任何前置檢查——builder 與 verification 全部跑完、候選已驗證通過後，
才在自動 foreign review 啟動時以 `foreign-review-config-error:unsupported project tier: None` 落入
`needs_human`。fail-closed 本身正確，錯的是**失敗得太晚**（fail-late），把整輪建置與驗證成本浪費掉。

漸進導入 canonical manifest 的 repo 最容易中招：本來無 manifest 時走 `shareable` 預設，
一旦補上一份其他欄位都合法的 manifest，foreign review 行為反而從預設變成晚期設定錯誤。

## Tasks

- [ ] 前置驗證：foreign review 為 required 的流程，在 builder dispatch 前就檢出缺失／非法 `tier`（deck／doctor／preflight 任一具權威的前置點，與既有 readiness 診斷同層）
- [ ] 診斷訊息點名選定的 manifest 路徑與允許值（`shareable`／`work`／`personal`），operator 不需翻 code 即可修正
- [ ] 已驗證通過的候選不得僅因此前置未驗而新落入 `needs_human`
- [ ] 測試涵蓋四情境：無 manifest、canonical manifest 缺 `tier`、非法 `tier`、合法 `shareable`
- [ ] 裁決並文件化語意：缺 `tier` 究竟是「沿用無 manifest 的 shareable 預設」或「必填」——擇一並在 policy 文件與診斷訊息中一致呈現
