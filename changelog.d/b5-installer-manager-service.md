修正 #878：`cortex install service` 會 enable `<instance>-manager.service`，使 manager 隨 user systemd 的 `default.target` 啟動，不再只依賴已標示 deprecated 的 timer。
