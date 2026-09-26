#877 新增 `cortex service ensure-running` 冪等入口，優先啟動可用的 systemd user units，否則以目前 Cortex interpreter 本地啟動 manager 與 monitor，並輸出 JSON 狀態封套。
