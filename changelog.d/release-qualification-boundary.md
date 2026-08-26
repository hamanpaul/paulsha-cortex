將 package release gate 改為 exact-SHA、無 secrets、無外部寫入的 deterministic systemd
qualification，並把真實 provider、GitHub 與 full-dispatch 驗證拆成不阻擋 release 的 protected
deployment canary；evidence schema v2 以 profile 防止兩種結果互相冒充。兩條 workflow
也會在 source bundle 建立前強制取得並驗證完整 Git history，避免 shallow bundle 在安裝後
repository `fsck` 才失敗。qualification evidence 會寫入獨立的一次性 Docker volume，再由
runner 匯出；不新增 writable host bind，且避開 Docker archive API 無法讀取 container
`/run` tmpfs 的限制。validator 會比對 canonical evidence tree 與 artifact inventory 的完整
檔案集合，未列管檔案、symlink 或列在 tree 外的 artifact 都會 fail closed。
