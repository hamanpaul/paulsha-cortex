---
status: accepted
work_item: pretooluse-capacity-gate
---

# pretooluse-capacity-gate Specification

Issue #136 MVP：在昂貴的 subagent/headless spawn 前，強制查一次既有的 control
`status.json`，manager daemon 忙碌時改為詢問（`ask`）而非無條件放行。issue 本文
提出的是升級版（weight vs headroom 比對，見 #209），但作者本人在 comment 中把
本票範圍收斂為「補 ad-hoc 破口」的 MVP：只看 `daemon.idle` 布林。本 spec 記錄
這個收斂後的範圍與邊界，不含 #209 落地後才會有的 sizing/headroom 抽象。

## 背景：三層閘的脈絡

issue #136 comment 描述過容量治理的三層閘：

1. **eligibility**（本票不管）：這個 slice/work item 是否符合被派工的資格。
2. **admission**（本票是這層的 ad-hoc 補丁）：現在要不要真的多開一個
   subagent/headless session——manager daemon 是否忙碌。
3. **routing**（本票不管）：開了之後派到哪個 executor/model。

manager daemon 自己的 tick fanout 早已有 idle gate（`manager_daemon.py` 內既有
邏輯，非本票範圍）。本票補的破口是：**agent 在互動 session 中自己手動呼叫
`Task`/`Agent` 工具，或用 `Bash` 起一個 `codex exec`/`claude -p`/`copilot -p`
headless session**——這條路徑完全繞過 daemon 的 fanout gate，過去沒有任何一層
會去查 `status.json`。

## Requirements

### 判準：daemon.idle 布林（MVP，非 weight/headroom）

`paulsha_cortex.porcelain.capacity_gate.classify_tool(tool_name, tool_input)`
SHALL 把 `Task`/`Agent` 工具一律視為昂貴 spawn；`Bash` 工具 SHALL 只在
`tool_input["command"]` 符合 headless launcher 樣式（`codex exec`、
`claude -p`/`--print`、`copilot -p`/`--prompt`/`suggest`）時才視為昂貴 spawn。
樣式常量 SHALL 可用環境變數 `PSC_CAPACITY_GATE_BASH_PATTERN` 覆寫，作為
未來擴充的可插拔點，不必改程式碼。

`evaluate_gate(tool_name, tool_input, status)` SHALL 對非昂貴 spawn 的工具
回傳空 dict（allow，no-op）；對昂貴 spawn，SHALL 讀 `status["daemon"]["idle"]`：
`False` 時回傳 Claude Code PreToolUse hook 協定的 `ask` 決策；`True`（或
daemon 存在且非 busy）時回傳空 dict（allow）。

**本票明確不做**：weight 對 headroom 的向量化比對（那是 #209，目前仍 OPEN、
main 上無對應 sizing/capability envelope 實作）。`evaluate_gate` 的 status
參數形狀刻意貼齊既有 `read_status()` 回傳的 `daemon` 欄位，日後 #209 若要把
判準從單一布林升級為 weight/headroom 比對，可在同一個函式簽章內擴充，不需要
改 hook 協定或呼叫端。

### 安全預設：degraded 視為 busy

`status["degraded"]` 為 True（例如 `status.json` 缺失、daemon pid 已死、
snapshot 過期）時，`evaluate_gate` SHALL 視為 busy 並回傳 `ask`，即使
`daemon.idle` 欄位本身讀不到或找不到。理由：degraded 代表「不知道 daemon
是否忙碌」，不是「已知道 daemon 不忙」；容量不變量必須被強制執行，讀不到
狀態時靜默放行等同於讓這個閘門形同虛設。

### CLI 入口

`cortex capacity-gate check` SHALL 從 stdin 讀 Claude Code PreToolUse hook
傳入的 JSON payload（`tool_name`/`tool_input`），呼叫既有
`paulsha_cortex.control.client.read_status()` 取得即時狀態，執行
`evaluate_gate`，把決策 JSON 印到 stdout。SHALL 支援 `--status-path` 覆寫
狀態來源路徑（測試/fixture 注入用；指定路徑但檔案不存在時視為 degraded）。
exit code SHALL 固定為 0——決策完全靠 stdout JSON 表達，不靠 exit code，
避免誤觸發 Claude Code 的「hook 執行失敗」分支（那條路徑的語意是擋下工具
呼叫，不是「詢問使用者」）。

以 B1 既有的 porcelain 命令註冊表登記（`_FAMILY_MODULES` 加入
`paulsha_cortex.porcelain.capacity_gate`），`cortex --help` 自動列出，
無需另外維護 `cli.py` 的 help 字串。

### claude.json 模板：僅供未來消費，本 repo 不即時生效

`paulsha_cortex/scripts/hooks/claude.json` SHALL 新增 `PreToolUse` 區塊，
`matcher` 涵蓋 `Task` 與 `Bash`，`command` 呼叫 `cortex capacity-gate check`，
結構比照既有 `codex.json` 的 CamelCase 巢狀 + matcher 樣式。

**本 repo 不自動把這個模板寫入使用者 live 的 `~/.claude/settings.json`**——
`paulsha_cortex/deploy/installer.py` 目前只 reconcile codex 端的
`hooks.json`，完全不碰 claude 端設定；PreToolUse 寫入使用者 live settings.json
的最終切點屬於 paulshaclaw thin install 的職責（issue #136 triage comment
已明確裁決），非本 repo 範圍。本 spec 只交付模板 + gate 判斷邏輯 + CLI 入口，
不代表「已生效」。

### 限制

- 純函式（`classify_tool`/`evaluate_gate`）不觸碰任何全域狀態，方便測試；
  CLI 層才呼叫 `read_status()`/讀檔。
- stdlib-only（`re`/`json`/`argparse`），不引入新依賴。
- TDD：`tests/test_porcelain_capacity_gate.py` 涵蓋 busy/idle/degraded/
  non-gated-tool 各分支的純函式層與 CLI 整合層；
  `tests/test_coordinator_hook_templates.py` 驗 `claude.json` 的
  `PreToolUse` 區塊存在且指向 `cortex capacity-gate`。
