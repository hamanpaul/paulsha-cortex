---
status: accepted
work_item: workflow-terminal-jsonl-framing
---

# Workflow terminal JSONL framing 自有 OpenSpec 設計

與 [spec](proposal.md) 同屬 owner [#860](https://github.com/hamanpaul/paulsha-cortex/issues/860)。Root 於 2026-09-08 依 fresh `bounded_contracts_review` 的 PASS／0 BLOCKER-MAJOR 接受內容供 repository intake；以下仍是未來實作，不是已部署行為，也未因此完成產品 freeze／dispatch／qualification。

本文件完整承接 [原 accepted design](../../../docs/superpowers/specs/workflow-terminal-jsonl-framing-design.md) 的 D1–D6，並非要求 builder 在 freeze 後新增 planning 文件。這份 upfront baseline 與 [proposal](proposal.md)／[tasks](tasks.md) 一起交 root 做 integration review；不是新的 production 範圍或已完成的產品 gate。

## Decisions

### D1 只修 record framing 與必要的讀取語意

在 `_extract_terminal_json` 內以 `Path(log_path).open(encoding="utf-8", newline="")` 的 context manager 讀取文字，再用 literal `content.split("\n")` 切 records。Python 3.10–3.13 的 file-open newline 參數可用；不使用只有較新 Python 才支援的 `Path.read_text(newline=...)` 呼叫。保留原 OSError／UnicodeDecodeError → `workflow terminal log unreadable` 映射，handle 正常關閉。

這個小讀取變更是必要的：原 `Path.read_text` 使用 universal newline，會先把裸 CR 變成 LF；只換 split 方法無法實現 spec R1 的 literal LF／CRLF 邊界。選擇 `newline=""` 不轉換資料，CRLF 的 CR 留在 JSON 行尾作合法 whitespace，既有 fence regex 自行支援 CRLF。裸 CR 相接的兩筆物件仍是一個無效 JSON record，不額外提供 fallback。

不以 `str.splitlines()`、regex `\s`、Unicode normalize、`strip`／`rstrip` 改寫 record 或 payload。原有「空白行是否略過」的判斷可留，但傳入 `json.loads` 的非空 record 不經內容 strip。全檔 read／反向 list 掃描的記憶體複雜度不變；沒有新增 queue、thread 或持久狀態。

### D2 保留現有採信層次

資料流是「Manager 選 same-era／same-Candidate 最新 job → 讀 job.log_path → 實體 LF record → 既有 carrier extraction → terminal shape → gate／phase schema／report／identity 採信 → Manager evidence」。本票只動第二、三段的 framing；state writer 與模型 wrapper 不改。

當前 source 錨點：`manager.py:10488–10508` 選 job，`:6321` 讀 job.log_path，`:4355–4405` 抽取，`:4441–4454` shape，`:6330–6334` gate，`:6358–6392` verify schema/report。Production 修改不得超出 `_extract_terminal_json`，caller 的真接線以 tests 驗，不藉 caller tests 把 production scope 算成跨模組。

### D3 JSONL 與 JSON text 兩層序列化

矩陣必涵蓋 inner `result` 字串／outer provider record 各自 `ensure_ascii=True/False`；`structured_output` 副本的值也可含 NEL/LS/PS。對 fixture 最終 decoded payload 做深度相等比較，而非只檢查 status；原始 payload 值必須保留。

反例：只 escape inner result，outer serializer 對 `structured_output` 輸出 raw NEL，舊 `splitlines()` 仍會破壞整筆 record。合法產品修復不能依賴 provider 永遠 ASCII serialization。事故暫時恢復若採 ASCII 名稱 `U+0085` 是刻意改用說明文字的新 terminal，不是本票 fidelity oracle，也不允許回寫原檔。

### D4 信任邊界與精確列管殘餘

| Surface | 本票保留／驗收 | 明確未解決的範圍 |
|---|---|---|
| `_parse_terminal_json_text` (`manager.py:4408–4438`) | 完整 JSON、既有末尾 JSON/fence 路徑；嵌入且後接非 terminal 敘事的假 evidence 不新增採信 | 不重新設計所有「最後 payload」語意 |
| `_unwrap_structured_output` (`:3920–3938`) | 只一層、只有 terminal_contract 白名單；未知或多層 wrapper 仍拒收 | 不新增任意 `structured_output` fallback |
| tool `data.content` (`:4384–4388`) | 僅 `assistant.message` 可讀此 carrier；`tool.execution_complete.data.content` 保持拒收，尾端 spoof 不覆蓋真正 terminal | 當前 generic `result/content/message/text/response` (`:4389–4392`) 沒有 event-type allowlist；synthetic tool 的頂層 `text` 本來可被抽取。這是既存另一個 carrier-authentication 契約問題，交 root 的 R13/R06 列管；本票不聲稱所有 tool spoof 都拒收 |
| terminal shape／schema | 型別、enum、exact key set、report path/size、Candidate／era／ledger 全不放寬 | parser 能讀不保證 schema／gate／獨立 reviewer 採信 |
| reverse scan | 尾端非 terminal／非 JSON 行維持略過，回到較早合法 terminal | 較新 malformed terminal 是否應阻擋較舊 terminal 是現有選擇政策，不在 framing 修正中暗改 |
| resource／live | 靜態有限 fixture 的全檔讀取與 hash 可重播，無額外副作用 | 全檔 O(bytes) 記憶體、live concurrent append／rotation、部署與其他 parser 的同類缺口另列管；不能宣稱本票提供 streaming memory bound 或全系統修復 |

這些殘餘有明確既存 source 邊界；此次 diff 不新增 carrier 類別或 side-effect authority。若 reviewer 認為某一殘餘會令本修補本身不安全，須指出具體新增影響，root 再裁決擴 scope／另件，不以 accepted 標記遮蔽。

### D5 測試與交付邊界

重用 pytest／`tmp_path`／`unittest.mock`，集中新增 `tests/test_workflow_terminal_jsonl_framing.py`。先做 Unicode record-fidelity RED，再做讀取與分行兩個 seam 的最小修正；實際行數由最小且可讀的 context manager 決定，不為行數省略資源關閉。既有 carrier tests 在 `tests/test_workflow_production_wiring.py:4318–4484`，wrapper tests 在 `tests/test_terminal_result_contract.py:472–487`；維持原拒收 oracle。

合成 fixture 是 CI authoritative regression。私有 #94 原檔只允許額外唯讀 replay，讀前後 SHA256 一致；不能把原 log 複製進 repo、不能要求 CI 存取 live root。Memory-only monkeypatch 只證明 seam，未來產品必另跑真 `tmp_path` file-open/newline/error tests。真 CLI help 以候選 interpreter、checkout 外 cwd 實跑；不接 live control。

### D6 Sizing、審查與 authority

唯一 production function 位於單一模組，因此 domain_breadth=0；無新增 shared state／writer／concurrency 轉移，因此 state_consistency=0。invariant_count=10 對應 spec R1–R10；artifact_classes 完整含 source/tests/documentation。

完整 `fix-standard` 保留 gate_spine=2、cards=9、persona bindings=9、全部 R-09/R-16/R-19。Author 首稿當時三件組為 draft，真 completeness=false；舊算法因未 accepted 而較低的 raw stability 數字從未授權進件，當時 accepted 分數只是記憶體 counterfactual。現在依 root 接受更新三件組後，現行真 helper 為 completeness=true、0/0/2/2/2=6 Yellow；#831 真載入且完整 accepted 後仍僅條件投影 0/0/2/0/2=4 Yellow。詳細歷史與當前 actual／negative controls 見 report；不改低數值、刪欄位或縮 combo 規避，pure helper 也不代替正式 freeze／readiness／qualification。

第一輪獨立審查標準：未處置的缺陷／缺口 → FAIL；已明文承認、影響分析有界且列管的殘餘風險不單獨構成 FAIL；反對某殘餘時須具體反駁其影響分析。本次 accepted 來自 root 完整閱讀與獨立 reviewer 的內容審查，不是作者自批；root 已建立並讀回 #860，後續 repository intake 與正式 authority 仍由 root 負責。

## Upfront planning 與 checkbox 契約

正式映射本 child OpenSpec 後，artifact rows 先加入 proposal(spec)／design(design)／tasks(plan)，最後加入 workstream todo(plan)。Yellow helper 使用第一份 plan，sizing snapshot 使用最後一份 plan，因此 [tasks](tasks.md) 與 [原 todo](../../../docs/superpowers/workstreams/workflow-terminal-jsonl-framing/todo.md) 都完整宣告 domain=0、state=0、invariant_count=10、artifact_classes=source/tests/documentation；兩份均保留 R-09/R-16/R-19 與 R1–R10 coverage，不能以空 shell 取得 gate ready。

正式 freeze 之後，operator 上的 authority baseline bytes MUST 保持原樣。Builder 只在 candidate 更新已獲證據的 tasks/todo checkbox；`kind=plan` 且 basename 為 tasks.md／todo.md 的 checkbox-only 差異才可由既有 tolerance 採信。Scope、文字、frontmatter、proposal/design/spec delta 不能暗改；有內容變更需求先回 root 走正式 authority 流程，不 patch frozen bytes。本 child active tasks 只列 pre-archive 工作；archive 本身、產品獨立 review／CI、merge／closure／runtime qualification 等下游 pending 以 prose 留存，不要求自己的 archive 先已完成才能 archive。完整交付仍受原 todo T10 與正式 Cortex gates 約束。
