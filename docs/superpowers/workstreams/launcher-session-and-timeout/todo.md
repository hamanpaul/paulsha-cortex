---
status: accepted
work_item: launcher-session-and-timeout
---

# Headless launcher session（保留 canonical work_id）

## Boundary

- 唯一 issue：`hamanpaul/paulsha-cortex#823`。#824 已拆至
  [agy-print-timeout-only](../agy-print-timeout-only/todo.md)，其 timeout／parser／
  值域及 #851 前置驗收均保留在 child；本工作不再實作或關閉 #824。
- 相關但不在本票關閉：#813／#568／#826。開發基底必須包含已合併 #820 的
  AGY JSON terminal／argv 修正；不得覆蓋或重新引入舊 probe 形態。
- 範圍限 `coordinator/launcher.py` 的兩個 headless Popen spawn 點及
  session／process-group 相應測試與文件；timeout 專用解析／傳遞已移交 child。
- `start_new_session=True` 隔離 session／process group，**不隔離 systemd cgroup**；
  若 Manager unit 的 KillMode 涵蓋整個 control-group，重啟仍可能終止 direct job。
  本票不承諾 daemon restart survival，該要求交完整 refine plan 的 runner／instance
  ownership 工作與對應服務整合測試；不可用 pgid 測試代替 cgroup 證據。
- 不改 dispatcher spawn（該處沒有 Popen）、effort、gate timeout 預設、Manager
  kill/cancel 操作面、LaunchHandle pgid 欄位；不為其他 executor 加 AGY 專用旗標。
  不操作共享 runtime-main、服務或正在執行的其他 repo job。

## Tasks

- [ ] 在 `SubprocessLauncher.launch` 共用 `popen_kwargs` 加
      `start_new_session=True`；正常及只針對 stdin TypeError 的重試路徑都保留。
      direct／systemd-run／systemd-template 三個 runner 包裝層，以及現有五個
      executor，必須使用同一設定；不放寬現有 TypeError 判斷以吞掉其他參數錯誤。
- [ ] 新增 fake Popen 測試：codex direct `bash -lc`、claude stdin=PIPE、
      stdin 第一次 TypeError 後的第二次 spawn，均捕獲 start_new_session=True。
      systemd-run／template 模式沿既有 `_RecordingPopen` fixture 驗最外層 spawn，
      不宣稱此斷言代表內層 job 的 cgroup／PGID 身分。
- [ ] 新增 `tests/test_coordinator_launcher_session.py`，只使用測試 subprocess，
      不呼叫模型 CLI：以同 kwargs 形狀啟動 `bash -c 'sleep 30'`，斷言
      `os.getpgid(proc.pid)==proc.pid` 且不同於測試 process group；向子 process group
      發 SIGTERM，5秒內 reap，測試進程仍存活。以 try/finally 清理本測試建立的子程序，
      不碰 Manager／真實 job。此案例只證明 process-group 隔離。
- [ ] 搜尋並更新固定 keyword-only fake Popen 以接受 start_new_session 或 **kwargs：
      基底約為 `tests/test_coordinator_launcher.py` 19處及
      `tests/test_headless_claude_hook_506.py` 3處，數量以實際 base 重新確認；保留
      原始安全斷言。已接受 **kwargs 的 `_RecordingPopen` 不無謂改寫。
- [ ] 保留 timeout child 的非 AGY argv 無專用旗標契約；既有 launcher、
      headless hook、trust-root runner/template、reviewer downgrade、accounting 與
      #820 planning/AGY 回歸測試維持綠燈。
- [ ] 文件與 changelog 交代 process-group／cgroup 邊界及 timeout child 移交；
      新增 `changelog.d/launcher-session-and-timeout.md` fragment 並同步
      `CHANGELOG.md [Unreleased]`。透過 Cortex 記錄 RED／GREEN、
      CLI help／完整 gates、review、merge 與 installed 驗證；
      尚未執行的 smoke 不得填成通過。
