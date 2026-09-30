### Added

- **#1222 claude 納入額度觀測**：`provider_read_contract("claude")` 改為 supported。periodic reconcile 從終局 claude job 的 stream-json `rate_limit_event` 收割五小時／七日的剩餘百分比，寫進 quota observation ledger，並支援 identity binding 與 coverage gap。這是被動來源：只有 claude job 跑過才會更新，逾 TTL 回到 unknown；終局時間加 TTL 已過期的 job 不重讀 log。quota enforce 手冊同步更新（#1222）。
