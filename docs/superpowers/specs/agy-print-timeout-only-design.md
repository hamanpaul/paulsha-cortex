---
status: accepted
work_item: agy-print-timeout-only
authority_state: proposed-unregistered
dispatch_readiness: blocked-dependency
depends_on:
  - agy-probe-construction-containment
dependency_issues:
  - "hamanpaul/paulsha-cortex#851"
---

# AGY print timeout 單模組設計

## Decisions

### D1：只有 launcher 改 production

原 author 以 `25c9a4e4040476523d0bb5f2132236f87b8d65f8` 為規劃 base。唯一 production
寫入目標是 `paulsha_cortex/coordinator/launcher.py`。其第 13 行本來就 import
`gate_ledger`，直接使用現有 `_gate_timeout()` 即可；不新增第二份 gate parser。

資料流為 caller env → `resolve_agy_print_timeout` → canonical `Ns` →
`build_agy_argv` → 既有 shell wrapper → Popen。resolver 不存取檔案／服務／registry。
`build_agy_argv(None)` 與正式 launcher 的顯式值共用同一解析與最終範圍檢查。

`SubprocessLauncher` 的工作目錄、bundle、gate、sentinel、降權與 permissions
流程保持原樣。timeout 在 manager 組 argv 時已物化，因此不新增 job env allowlist。
若有 evidence 顯示必須改 `gate_ledger.py` 才能完成，停止本切面並回 root 重裁決。

### D2：解析、正規化與溢位防護

- module-local 常數：env 名稱、600 秒 buffer、`9223372036` 整秒最大值。
- env 使用 key presence 區分未設定與空白；strip 後 ASCII digits only；前導零
  正規化，不接受符號、小數、指數、unit 或 Unicode digits。
- 先正規化 digit string，再以長度與同長 lexicographic 比較最大值，最後才
  `int()`；超長數字不依賴 Python 的字數上限。零值明確拒絕。
- keyword 使用 `re.fullmatch(r"[1-9][0-9]*s", value)`，不使用會容許末尾
  newline 的 `re.match(...$)`；型別先驗證，再檢查整秒最大值。
- override 存在且合法即返回，不呼叫 gate helper。未設定時直接呼叫既有 helper，
  取 `max(DEFAULT_GATE_TIMEOUT_SECONDS, gate_seconds) + 600` 後套用上界。
- valid gate `9223371436` 是無 override 時最大可推導值；下一秒使最後 duration
  超界，launcher 回 `ValueError`。不變更 gate helper 的 fallback 或設定本身。

### D3：argv exactness

```text
planner / reviewer / write-forbidden:
  agy --print <one-prompt> --mode plan --sandbox [--add-dir ...]
      [--output-format json] --print-timeout <Ns> [--model ...]
builder / commit-required / unsafe:
  agy --print <one-prompt> --mode accept-edits --add-dir <worktree>
      [existing git directories / unsafe flag]
      [--output-format json] --print-timeout <Ns> [--model ...]
```

插入位置保留原本所有 argv prefix slices。`json_envelope=False` 只控制 JSON
flag，timeout 仍存在。`SubprocessLauncher` 只向 agy builder kwargs 新增
`print_timeout`；不把它塞給其他 executor 或增通用 `extra_args` 逃生口。

Fake Popen capture 不使用 `script.split(';', 1)` 擷取命令，因 prompt 本身
可以有分號。以 `shlex.shlex(script, posix=True, punctuation_chars=";")` 並設
`whitespace_split=True` 取得保留引號语意、將 shell 分隔符獨立切出的 tokens；
避免 model 缺省時把 `2400s;` 誤認成 timeout 值。再以 builder kwargs capture
證明精確傳遞；另有帶分號／引號 prompt 的直接 argv 測試證明 prompt 單元素契約。
每種形狀檢查 flag count==1。

### D4：測試與 CLI proof 分離

pytest 全為 offline fake contract tests，主要放在
`tests/test_coordinator_agy_launcher.py`，另在既有
`tests/test_coordinator_launcher.py` 增非 AGY regression assertion。
不加 #823 的 signal／session 測試。

resolver matrix 包含 spec R2/R3 所有列；額外驗證 keyword `1s`／max／max+1、
末尾 newline、非 str，以及 5000-digit env／keyword 均為 `ValueError`。
以 monkeypatch gate helper 證明無 override 時走既有 helper，override 時不走。
保留 existing `DEFAULT_GATE_TIMEOUT_SECONDS=1800` 與 helper fallback 的 source
hash，不修改其測試 oracle 以迎合新 resolver。

CLI proof 由 candidate evidence 執行，不進 pytest。先讀實際 `agy --version`，
無 credential env／無 prompt 下令 parser 在未知 flag 必停：

```bash
env -i HOME=/dev/null XDG_CONFIG_HOME=/dev/null PATH=/usr/bin:/bin \
  "$AGY_BIN" --print-timeout 2400s --cortex-timeout-parser-sentinel </dev/null
```

`AGY_BIN` 是 operator 已解析且版本已記錄的絕對可執行檔路徑，不由模型自行下載。
此命令故意不帶 `--print`、`--help` 或任何 prompt，unknown flag 位於 timeout
之後。完整 drain stdout/stderr 並保存實際 exit code，不用 `head` 截流造成
SIGPIPE。可用 `subprocess.run(capture_output=True, stdin=DEVNULL, env=...)`
在記憶體檢查，無需保存完整 help；不修改 HOME 本體或任何設定。

| value | 真正預期 |
|---|---|
| `abc` | exit 2；timeout invalid duration |
| `2400` | exit 2；timeout missing unit |
| `2400s` | exit 2；只到 unknown sentinel flag |
| `9223372036s` | exit 2；只到 unknown sentinel flag |
| `9223372037s` | exit 2；timeout invalid duration |

這個對照證明 timeout parser 接受 canonical 合法值並拒絕溢位；不聲稱執行成功、
模型回覆、實際 deadline 等待或完整 argv 的 headless E2E。

### D5：bootstrap 與保留項

新增必須先完成的 dependency：`agy-probe-construction-containment`，唯一 owner
為 [#851](https://github.com/hamanpaul/paulsha-cortex/issues/851)，已由 root
建立並已納入本 PR repository intake。本 child #824 則刻意不登錄、仍 blocked-dependency。
在原 author 核對的 runtime 中，
`model_identities.py:1071-1078` 的 argv construction 在既有 smoke try 外，
timeout resolver 的 `ValueError` 因此可能從 `planning_runtime.py:1664` 穿透，
連 ready 的非 AGY primary 都無法取得 runtime。R1 已獨立重現；timeout-only
不可先 freeze／dispatch，不能以殘餘接受或不驗證 probe env 的方式放行。
該 dependency 只改 `model_identities.py`，將 construction 納入既有 try／
`_failed_agy`；本 child 仍只改 launcher。待 root 核對 containment candidate、
實際 runtime 與 frozen base 證據後才能解除 block。

containment 的獨立驗收用 builder fault injection，不反向等待尚未落地的
timeout resolver。本 child 落地後須補真非法 `PSC_AGY_PRINT_TIMEOUT` 的
direct-launch／capability-probe雙路徑測試，並用isolated roster與fresh tmp cache
走真runtime constructor證ready非AGYprimary可建runtime；不mock整個probe，
不跳過probe的env解析。測試可加到既有model identities／planning runtime測試檔，
production仍只改launcher；若containment base不符就停止，不在本child偷修另一模組。

原 author 時期母 work item `launcher-session-and-timeout` 映射 #823/#824；
root 已在本 PR 移除其中 #824、保留 #823。本 child 唯一owner仍#824，但刻意
不登錄timeout child，待前置真完成後再正式進件／freeze。本文件整合不修改
`.cortex/work-items.yaml`、issue、source owner、active run 或 runtime。
既有來源不得同時把同一 #824 授權給兩個 active work item。

Root 已定案本 child 只映射 #824。`manager.workflow_build_branch()` 依最小 issue
與 work_id 固定回 `feature/824-agy-print-timeout-only`，不能由 todo 任意換名。
canonical fragment 為 `changelog.d/agy-print-timeout-only.md`；母
`changelog.d/launcher-session-and-timeout.md` 的責任留 #823，不複製兩份 fragment。
#824 已由 root 正式更新，唯讀 readback 的 updatedAt 為
`2026-09-07T13:40:04Z`：AC5 已同步此名稱與 branch，AC6 已改 sentinel
parser proof，另明列 fullmatch／range 與 #851 dependency。timeout child的
registration刻意保留未登錄，block不解除；本author不改issue、registration或Manager。

原author完整三件套／記憶體移除marker控制組在runtime `79ba6447` 得6 Yellow，
不需要先修#831才能算分；目前帶marker的真文件不完整、4 Yellow，並非dispatch-ready。
Yellow strong plan review、provider capability/preflight、exact frozen base 仍是
派工必要條件。若用 AGY 自己實作此 child，舊 runtime 尚有五分鐘上限；應由 root
使用已授權的可用 builder route，或先完成有界 focused RED，不能讓此 child 又以
未修改的 timeout 依賴自己的修正結果。

實作完成、candidate test PASS、merged、installed、live workflow retry 是不同狀態。
本 child 的 code acceptance 到 candidate／exact-head gate；安裝與對 #831 的
正式 retry 留給另行已授權的操作，不包成 production scope 擴張。

## Current integration gate

root於#852 merge `984fce5b`的整合worktree登錄#851，故意不登錄本child。
runtime79忽略workflow文件的`dispatch_readiness`／`dependency_issues`，
`depends_on`僅舊slice lane使用；不能把這些frontmatter當正式依賴閘。
todo的root marker必須保留：真completeness=false、plan blocking-decision、
stability=0、total4 Yellow；surface-only gate卻仍ready／envelope bypass。
即使不完整，start也可能走brainstorm，故不宣稱marker零派工。
root當次核對#824/#851 labels皆空、無相關run，且auto新work需
`cortex:auto-on-going`；維持不登錄、不auto-label、不start #824才是本次
操作邊界。前置產品、預定base和loaded runtime均通過後，再由root解除marker、
重接受並正式登錄；本輪不修改任何resolver／test契約或0／0／7宣告。
