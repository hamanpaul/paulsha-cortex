# #879 monitor 檔案事件 convoy 修正

monitor 在檔案事件快速湧入時，原本每個 project event 都建立一條 refresh `Timer` 執行緒，造成執行緒與資源無界累積。根因是事件 callback 直接負責 debounce timer 的建立，而非只提交有限的 refresh 工作。現在改由單一 worker take-and-clear 合併同一輪的 pending projects，再只做一次 watch/work-model publication；stop 後拒收 late event。另新增 `monitor.thread_count_warn_threshold`（預設 `200`），thread count 警告以 60 秒節流且不主動干預服務。
