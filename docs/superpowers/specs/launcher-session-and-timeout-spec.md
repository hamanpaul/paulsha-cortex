---
status: accepted
work_item: launcher-session-and-timeout
---

# Headless launcher session 規格

## Authority

唯一 issue owner 是 [#823](https://github.com/hamanpaul/paulsha-cortex/issues/823)，canonical
work_id 保持 `launcher-session-and-timeout`。名稱保留不代表重新納入 timeout；
[#824](https://github.com/hamanpaul/paulsha-cortex/issues/824) 的 AGY timeout 與
[#851](https://github.com/hamanpaul/paulsha-cortex/issues/851) 的 probe containment 均另案。
本三件組承接 root 已裁決的 session-only todo，`accepted` 是內容狀態。本 PR 是 repository
intake，仍 unfrozen／未 claim，不是正式 freeze 或 dispatch；現行 sizing 維持 6/Yellow。
正式 builder branch 為 `feature/823-launcher-session-and-timeout`；fragment 為
`changelog.d/launcher-session-and-timeout.md`。原四檔 authoring 未實作產品；本 PR 整合進件資料，
其後續 full preflight 由 root 另留證據，不把 author 檢查當產品或 PR CI 通過。

## Requirements

### R1 — 共用 spawn 參數

唯一 production 修改檔為 `paulsha_cortex/coordinator/launcher.py`。抽出同模組的
`build_headless_popen_kwargs(*, cwd, env, executor) -> dict[str, object]` 純 helper，
保留既有 cwd/env/stderr 與 claude stdin 條件，加入 literal `start_new_session=True`。
`SubprocessLauncher.launch()` 實際消費此 helper；正常及 stdin retry 的兩處 Popen
共用同一 kwargs，不能只在測試另造 dict，亦不能只修 direct 或某一 executor。
現有 `_ARGV_BUILDERS` 五個 executor 與 direct／systemd-run／systemd-template 三種 runner
的合法 launch 都適用；既有角色／權限前置拒絕保持，不為湊測試矩陣放行不合法角色。
目前 cg + systemd-template 缺 hardening profile，須在 Popen 前維持原拒絕；15 格 coverage
包含此負例，不等於新增 15 種可執行配置。

### R2 — stdin 的窄重試

保留現有 `TypeError` 訊息須含 `stdin` 才移除 stdin 並重試一次的範圍；第二次仍須攜帶
`start_new_session=True`，其餘 kwargs 不掉落。不得擴成吞任意 TypeError、移除 session flag、
重試未知參數或在第二次失敗後再降級；`start_new_session`／其他不含 stdin 的 TypeError
立即傳出，且沒有第二次 Popen。這是相容既有 fake 的有限退回，不增加新的 production fallback。

### R3 — 保留 runner／executor 語意

helper 只集中 spawn kwargs。runner 後續已有 env/stdin/cwd 覆寫、stdout log handle、
argv／shell mode、prompt 傳遞、角色 preflight、sentinel／log 與 LaunchHandle 既有欄位保持。
`PSC_JOB_RUNNER` 未設仍為 direct。systemd-run／template 測試捕捉的是最外層 Manager
exit-recorder shell／client wrapper 的 Popen；不能把其 session 斷言當內層 unit job 的 PID／PGID。
固定簽名 fake 必須接受新參數且保留原安全斷言；已收 `**kwargs` 的 fake 不必改簽名。
保留已合併 #820 的 AGY JSON／argv 與 planning 回歸，不改 effort、timeout 或 probe。

### R4 — 真 process-group 驗收

POSIX 隔離測試必須從 production helper 取得 kwargs，啟動測試自建且無網路／模型的 subprocess。
確認 child 的 PGID 與 SID 都等於該 child PID，並不同於測試 parent 的 PGID／SID；
只有 ownership 確認後才向該 group 發 SIGTERM。child 必須在 5 秒內被 wait/reap，parent
可繼續執行斷言。所有路徑含失敗的 finally 都有界收尾，只使用本測試持有的 process handle；
不能以 Popen mock 單獨替代真 PGID／signal 證據，也不能因負控制失敗誤殺 parent group。

### R5 — 明確作用範圍與交付

本件只隔離 session／process group，沒有 systemd cgroup 移動、Manager kill/cancel API、
LaunchHandle pgid schema、registry、dispatcher 或 service 變更。不承諾 Manager daemon restart
後 direct job 存活；unit 的 KillMode 若涵蓋 control-group，setsid 不能提供該保證。
該生命週期要求仍留完整 refine plan 的 runner／instance ownership 工作，不藉本票消失。
正式產品工作須完成 focused/full tests、CLI help、docs、changelog、既有完整 gates、
獨立 review、policy/CI、merge 與 checkout 外隔離 installed helper 測試，逐層留證；
不要求對 operator 的 Manager／真 job 作訊號、重啟或模型實測。

## Invariants

- I1：每次合法 headless Popen 嘗試均帶 `start_new_session is True`，包括重試。
- I2：stdin 之外的 TypeError 不被吞掉，無不帶 session flag 的 fallback。
- I3：各 runner／executor 的既有 argv、環境／I/O 與角色限制保持，不改 durable schema。
- I4：production helper 產生的新 session 讓 fixture child PGID/SID 等於自身 PID，group SIGTERM 不連坐 parent。
- I5：測試只 signal 自建且已驗所有權的 group，所有自建 process 與 PIPE 有界收尾。
- I6：process-group 綠燈不升格為 cgroup／restart survival、Manager cancel、timeout 或 probe 完成。

## Acceptance Criteria

- [ ] C01／I1/I3：helper 純資料正例與 launch recording matrix 盤點五 executor／三 runner 共 15 格；14 個現有合法配置測每次 Popen True，cg/template 保留 unknown-hardening 拒絕且 Popen=0。codex direct `bash -lc`、claude direct `stdin=PIPE` 與兩種 systemd wrapper 都有真 launch 接線斷言。新增 registry key 時重驗矩陣，沒有另一張 production 名單。
- [ ] C02／I1/I2：第一次 `TypeError("stdin")`、第二次成功，恰兩次嘗試且只有 stdin 被移除；第一次不含 stdin 的 TypeError 只有一次，第二次 TypeError 不被循環吞掉。
- [ ] C03／I3：重新盤點固定／寬簽名 fake，最小兼容修改並保留原始斷言；既有 runner env/stdin/cwd、hook、reviewer/accounting、安全 preflight 及 #820 回歸維持通過。
- [ ] C04／I4/I5：Design D3 真 fixture 驗 PGID、SID、group SIGTERM、parent continuation、5 秒 wait/reap 與 finally；去掉 helper session flag 的負控制在任何 group signal 前變紅，不能誤殺 parent。
- [ ] C05／I1/I6：刪除 launch 對 helper 的呼叫而只讓 helper 本身通過時，recording 測試必紅；只刪 retry 的 session flag、只修某 executor／runner 同樣可被抓到。
- [ ] C06／I3/I6：source scope 僅 launcher、正式 artifacts 與全 gate 相容；新增對應 fragment 及 Unreleased entry，R-09/R-16/R-19/R-22、symlink／VERSION／PR context 與 CI 有實際結果。CLI 只做既有 help，不新增 cancel 入口。
- [ ] C07／I4/I6：checkout 外以候選 wheel import 同一 helper，重跑隔離 fixture；核對 module path／候選 SHA，與合併、source suite、installed 及 live 分帳。此次 planning helper 結果不充當任何 C01–C07 的產品完成證據。

## Source condition

Root 回報本 PR 已將 #823 唯一 source 與 spec/design/todo path links 整合至本 work_id，
#824 已移出；本 PR 未登錄 #824 child，也未 claim 本件。repository links 不等 frozen authority。
真正 freeze 前仍由 root 重驗 #823 open／唯一 owner、本三件 exact refs/hashes、含 #820 的
fresh remote base，以及 reviewer input 的完整契約／獨立審查結果；#824 不得重新綁回本件。
純 completeness／sizing／plan gate 不讀 live registry，不能證明正式 admission 條件已成立。
