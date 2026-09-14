# #879 monitor 檔案事件 convoy 修正

monitor 在檔案事件快速湧入時，原本每個 project event 都建立一條 refresh `Timer` 執行緒，造成執行緒與資源無界累積。根因是事件 callback 直接負責 debounce timer 的建立，而非只提交有限的 refresh 工作。現在改由單一 worker 以每個 project 一份 pending 標記合併 debounce，並在 stop 後拒收 late event；另新增 `monitor.thread_count_warn_threshold`（預設 `200`）只記錄執行緒數警告，不主動干預服務。
