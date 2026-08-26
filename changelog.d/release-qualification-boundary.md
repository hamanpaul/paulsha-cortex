將 package release gate 改為 exact-SHA、無 secrets、無外部寫入的 deterministic systemd
qualification，並把真實 provider、GitHub 與 full-dispatch 驗證拆成不阻擋 release 的 protected
deployment canary；evidence schema v2 以 profile 防止兩種結果互相冒充。兩條 workflow
也會在 source bundle 建立前強制取得並驗證完整 Git history，避免 shallow bundle 在安裝後
repository `fsck` 才失敗。
