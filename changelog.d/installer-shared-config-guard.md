---
type: fix
scope: installer
---

`cortex install service` 對既有 `project-cortex.yaml` 採 append-only workspace
遷移，替換前建立 `.bak-*` 備份；不可載入的既有 project config 或 model identity
registry 會拒絕覆寫，跨 HOME 的 default agents root 需以 `--agents-root` 明確放行。
命中既有 workspace 時逐字保留 operator 條目與其他設定區塊，migration rollback
僅在 restore 成功時清理本次建立的 `.bak-*`，restore 失敗則保留備份並在錯誤訊息列出
復原路徑供操作員取回；若尚未取得備份則列出可能不一致的檔案，且備份 mode 不受
umask 影響而與來源一致；porcelain install 也會保留 installer 正常返回時產生的 stderr
診斷訊息；rollback 成功後若清理備份失敗則記錄 warning，不遮蔽原始 migration 例外。
rollback 失敗診斷改為逐檔綁定自身 backup 或明示內容只存在於記憶體中的 `previous`；restore
改以同目錄暫存檔搭配 atomic replace 並保留原檔 mode，project config loader 也會保留原始
驗證例外與原因。
既有 `project-cortex.yaml` 若為 symlink，append/replace 會在 mutation 前 fail-closed 並明示
路徑，保留 symlink 與其 target 不變。
