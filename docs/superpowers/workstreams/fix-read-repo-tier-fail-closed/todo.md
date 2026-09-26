---
status: accepted
work_item: fix-read-repo-tier-fail-closed
---

# fix-read-repo-tier-fail-closed Todo

`#492` 的歷史症狀：canonical `.project-policy.yml` 存在但缺 `tier` 時，`read_repo_tier()` 對 `None` 拒絕，
而前置檢查未涵蓋此條件，builder 與 verification 全部跑完後才在 foreign review 啟動時落入
`needs_human`。fail-closed 本身正確，問題是**失敗得太晚**（fail-late），浪費整輪建置與驗證成本。

漸進導入 canonical manifest 的 repo 最容易中招：本來無 manifest 時走 `shareable` 預設，
一旦補上一份其他欄位都合法的 manifest，foreign review 行為反而從預設變成晚期設定錯誤。

## 修正

`autonomy.dispatch_ready()` 對 required review 的 builder slice 在派工前驗證 tier，檢查位於建立工作區與 job 之前。缺 tier 或非法值會指出選定 manifest 路徑及允許值；無 manifest 維持 `shareable` 預設。非必要 review 不執行這項檢查。

## Tasks

- [x] 前置驗證：foreign review 為 required 的 builder slice 在派工前檢出缺失／非法 `tier`
- [x] 診斷訊息點名選定的 manifest 路徑與允許值（`shareable`／`work`／`personal`）
- [x] 缺 tier 時不建立 builder job／工作區，不會因此跑完建置與 verification 後才新落入 `needs_human`
- [x] 測試涵蓋無 manifest、canonical manifest 缺 `tier`、非法 `tier`、合法 `shareable`
- [x] 裁決採 fail-closed：已存在 manifest 必須有有效 `tier`；沒有 manifest 時沿用 `shareable` 預設，並於錯誤訊息列出允許值
