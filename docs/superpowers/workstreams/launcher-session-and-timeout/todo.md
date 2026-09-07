---
status: accepted
work_item: launcher-session-and-timeout
---

# Headless launcher session 與 AGY timeout

## Boundary

- Issues：`hamanpaul/paulsha-cortex#823`／`hamanpaul/paulsha-cortex#824`。
- 相關但不在本票關閉：#813／#568／#826。開發基底必須包含已合併 #820 的
  AGY JSON terminal／argv 修正；不得覆蓋或重新引入舊 probe 形態。
- 範圍限 `coordinator/launcher.py` 的兩個 headless Popen spawn 點及 AGY
  `--print-timeout` 解析／傳遞，加相應測試與文件。
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
- [ ] 實作前取得目前 AGY CLI version／help 的離線證據，執行
      `agy --print-timeout 2400s --help` 或等價不啟動 prompt 的參數 parser smoke，
      確認 Go duration `2400s` 被接受且 exit code 成功。若目前 CLI 不支援，停止
      該旗標變更並將版本／錯誤交主流程裁決；不可透過付費模型 prompt 猜測旗標。
- [ ] 新增 `resolve_agy_print_timeout(env: Mapping[str, str]) -> str`：顯式
      `PSC_AGY_PRINT_TIMEOUT` 為正整數秒，優先且不 clamp；未設時採
      `max(DEFAULT_GATE_TIMEOUT_SECONDS, PSC_GATE_TIMEOUT 或預設) + 600`，
      格式如 `2400s`。顯式空字串／非整數／0／負數拒絕；未設 override 時，非法
      PSC_GATE_TIMEOUT 同樣拒絕，不能默默壓低 gate 時間。
- [ ] `build_agy_argv` 新增 `print_timeout: str | None = None`；None 時自行依 env
      resolve，其他顯式值也驗證符合已證實的正 duration 契約。所有 planner／reviewer／
      builder／unsafe／write-forbidden／commit-required argv 形態都帶此旗標；
      `SubprocessLauncher` 對 agy 顯式傳入 resolve 結果，避免不同入口語意分歧。
- [ ] `tests/test_coordinator_agy_launcher.py` 的7個直接 argv shape 測試加入
      timeout 斷言，並守住 #820 的 JSON flag。兩個 env 都 unset→2400s；
      gate=900→2400s、gate=3600→4200s、override=900→900s；非法 override／gate
      與顯式 duration 負例均拒絕；合法 override 優先、不受未採用的 gate env 干擾。
- [ ] launch 傳遞測試沿 `test_agy_commit_required_launcher_emits_real_scoped_git_dirs`
      的 recording fake Popen，direct script argv 含 `--print-timeout 2400s`。
      不誤用只 stub `_ARGV_BUILDERS`、未記錄 Popen argv 的測試當 end-to-end 證據。
- [ ] 搜尋並更新固定 keyword-only fake Popen 以接受 start_new_session 或 **kwargs：
      基底約為 `tests/test_coordinator_launcher.py` 19處及
      `tests/test_headless_claude_hook_506.py` 3處，數量以實際 base 重新確認；保留
      原始安全斷言。已接受 **kwargs 的 `_RecordingPopen` 不無謂改寫。
- [ ] codex／copilot／claude／cg argv 均不得包含 `--print-timeout`；既有 launcher、
      headless hook、trust-root runner/template、reviewer downgrade、accounting 與
      #820 planning/AGY 回歸測試維持綠燈。
- [ ] 文件與 changelog 同時交代 process-group／cgroup 邊界、timeout 優先序及實際
      CLI 版本；新增 fragment 並同步 `CHANGELOG.md [Unreleased]`。透過 Cortex
      記錄 RED／GREEN、離線 CLI 合約、完整 gates、review、merge 與 installed 驗證；
      尚未執行的 smoke 不得填成通過。
