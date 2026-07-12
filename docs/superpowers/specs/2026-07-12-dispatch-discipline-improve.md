# Improve spec：強化 cortex 任務分派紀律（依 hippo multi-agent 實戰）（2026-07-12）

> Improvement 提案，非 greenfield。cortex 的 coordinator + persona 已經是一套多 vendor agent 分派系統；本 spec 把 paulsha-hippo 一次 9-issue 清零全程（多模型 co-work：copilot 實作／sonnet 驗收／Codex 異質 gate／frontier 硬修，跨多輪撞 limit、resume、洋蔥式剝 bug）**實測決定性的幾條紀律**落進 cortex 現有模組。方法論已固化於 `hamanpaul/paulsha-hippo` 的 `custom-skills/multi-model-orchestration`。

## 1. cortex 現況（實地盤點，非臆測）

cortex 已具備分派骨架，且相似度極高：

- `persona/personas.yaml`：roles = `manager`（allowed_phases: research/define/plan/build/verify/review/ship）、`builder`（build）、`reviewer`（review）；`enforcement: shadow`。
- `coordinator/launcher.py`：`build_claude_argv` / `build_codex_argv` / `build_copilot_argv` — **三家 vendor 都能 headless 起**。
- `coordinator/autonomy.py`：`scan_specs` + `parse_spec_frontmatter` + `_build_graph` + `detect_cycles` — 讀 spec frontmatter、建**依賴圖 DAG**、偵測 cycle（= 拓撲分派）。
- `coordinator/dispatcher.py`：`Dispatcher`、`_branch_for_task`（per-task 分支）、exit sentinel + `_pid_alive`（追蹤存活）。
- `coordinator/manager.py`：`GateRunner` protocol、`_default_gate_runner`、handoff manifest 的 `_satisfied_pred`。
- `coordinator/completion.py`：`classify_completion(exit_code, last_jsonl_line) -> 'done'/'failed'`。
- `persona/gate.py`：`evaluate_diff` → `build_verdict`；`persona/shadow.py`：`run_shadow_validation`；`persona/handoff.py`：manifest 交棒；`persona/guardrail.py`：per-persona 路徑/工具 guardrail。
- `coordinator/broker_reaper.py`：回收孤兒 codex broker（parent-based 偵測，委派 `scripts/reap-codex-brokers.sh`）。

**已經對得上的**：多 vendor launch、per-task 分支、DAG 拓撲、gate 概念、handoff、guardrail、reaper。這份 spec 不重造這些，只補「hippo 實戰證明缺了會出事」的紀律。

## 2. 五條實戰紀律 → cortex 的具體缺口

### P0-A｜完成 = 結果驗證，不是 exit code 分類

**hippo 實證**：agent 會 **exit 0／宣稱「done」但根本沒做完**——copilot 在一個 task 回了 `placeholder-not-final`、多個 agent 宣稱成功但測試是紅的。單看 process 退出與末筆 log 會把「假完成」當「完成」。

**cortex 缺口**：`completion.classify_completion(exit_code, last_jsonl_line)` 正是「exit code + 末筆 JSONL」判 done/failed——結構上無法分辨真做完與假做完。

**提案**：在 `classify_completion` 回 `done` 與 manager 接受交棒**之間**插一個**結果驗證關**（比照 hippo 的 driver verify）：對該 task 的宣告產物做客觀斷言——分支有新 commit、該 task 的測試實跑為綠、diff 與 task 意圖相符（可用 persona contract 的 `allowed_phases`/scope 界定該驗什麼）。驗不過 → 不是 `done`，是 `needs-fix`（進 P0-C 的 fix-loop）。`done` 這個字只能由結果驗證授予，不是 agent 自稱、也不是 exit 0。

### P0-B｜驗證 gate 異質 + fail-closed（整套架構的存在理由）

**hippo 實證**：全綠測試 + **同模型** 3-lens 審查放過的 15+ 個真 bug（secret redaction 可被 override 繞過、DB 非原子發布、YAML 被標準 parser 靜默截斷、lost-update race、init rollback 誤刪、硬中止 crash-consistency…），**全部是異質 vendor（Codex）gate 抓到的**。同 vendor 自審共享盲點；不同 vendor 才是真的第二意見。

**cortex 缺口**：兩個。(1) `personas.yaml` 沒有規定 `reviewer` 的 vendor 必須 **≠** `builder` 的 vendor——若兩者都派給同一家，gate 名存實亡。(2) `enforcement: shadow` = 目前 gate 是觀察模式，不擋。

**提案**：
- **異質綁定**：dispatcher 指派時強制 `reviewer.vendor ≠ builder.vendor`（cortex 三家都能起，天然可滿足）。builder 由誰做不限，但審它的 reviewer 必須跨 vendor。
- **fail-closed**：`reviewer` 的 blocking verdict **不可被 builder 側（同 vendor/同角色）以「可能誤報」為由否決**——只能 (a) 修好後重審、或 (b) reviewer 自己在複審撤回。把此語意從 `shadow` 升為對 blocking verdict 生效的 enforce（其餘 severity 可維持 shadow/observe）。
- **缺席顯性**：reviewer 起不來／逾時且復原失敗 → 標 `gate-absent`，不得靜默當通過；升級人工或明確記錄於交棒 manifest。

### P0-C｜有界 fix-loop + 誠實升級（洋蔥收斂）

**hippo 實證**：gate 每輪確認前輪已修、再剝出更深一個（且**修復可能引入新缺陷**，同 gate 再抓），跑到一輪 re-gate 乾淨為止；卡關就標 blocked、下游跳過，不硬推。

**cortex 缺口**：manager/gate 目前沒有明確的「reject → fix → re-gate ≤N 輪 → 否則 needs-human」狀態機。

**提案**：manager 的 job 狀態機加 `needs-fix → (builder 修) → re-gate`，上限（建議 2）；仍不過 → `blocked` / `needs-human`，DAG 下游依賴此 task 者標 `upstream-blocked`、不啟動。明文：一輪 re-gate 全綠才算 done；「已修得差不多」不是完成條件。

### P1-A｜persona 依比較優勢分派（roster）

**hippo 實證**：frontier 只做編排＋硬修（用量壓最低、最後才撞 limit）、bulk implement 丟 copilot/cheaper、異質 gate 丟不同 vendor、瑣事丟最便宜。角色錯配 = 又貴又容易撞牆。

**cortex 缺口**：`personas.yaml` 的 role→phase 有了，但沒有 **role→vendor/model（依比較優勢）** 的宣告。

**提案**：`personas.yaml` 每個 role 增 `vendor`/`model` 欄與「比較優勢」註記；dispatcher 依此起對應 CLI（`build_*_argv` 已備）。預設建議：manager=frontier、builder=cheaper/other-vendor、reviewer=**與 builder 不同 vendor**、瑣事=最便宜。與 P0-B 的異質綁定一致。

### P1-B｜任務狀態持久 → resume 不 restart

**hippo 實證**：長跑必撞 session/rate limit；靠 journal 把已完成步驟持久化、`resumeFromRunId` 續跑（已完成零成本跳過、已 merge 的批次硬標完成不重做），是活下來的關鍵。

**cortex 缺口**：dispatcher 用 exit sentinel + pid 追活，broker_reaper 回收死掉的——但 task 被 reap／agent 撞 limit 死掉後，是**從持久狀態 resume 到上次 checkpoint**、還是從頭 restart？handoff manifest 已是持久交棒點，但缺「resume 語意」。

**提案**：task 狀態（phase、已過的關、產物 ref）持久到 handoff manifest／journal；reaper 回收後，manager 重派時**從最後 checkpoint 續**，已完成的 phase/關不重跑；已 ship 的 task 標 terminal 不重啟。明文：resume 是常態路徑（天天走、被驗過），不是只有出事才踩的冷門分支。

### P2｜reap／kill 前驗歸屬（guardrail 強化）

**hippo 實證**：恢復序列差點誤殺兩個「孤兒狀」進程——實為別的專案 bot 與另一 agent 的 worktree job；照 name-pattern 殺會砸掉 live 工作。

**cortex 現況**：`broker_reaper` 已用 parent-based（reaper 的 `app-server-broker.mjs`）而非純 name-pattern，方向正確。**提案**：把「SIGTERM 前必驗 cmdline/cwd 歸屬、絕不只憑 pattern」寫成 `guardrail.py` 的明文原則，涵蓋未來任何新增的 kill 路徑（不只 codex broker）。

## 3. 非目標

- 不重造 dispatcher/persona 骨架（已存在，只補紀律）。
- 不強制所有 severity 都 fail-closed——只有 **blocking verdict** 升 enforce，其餘可維持 shadow/observe（漸進，不打斷現行 shadow 部署）。
- 不 bump `VERSION`（release 時機另定）。
- 不把某一家 vendor 寫死為唯一 reviewer——只綁「reviewer.vendor ≠ builder.vendor」的約束，不綁具體家。

## 4. 驗收（怎麼知道成功）

- **假完成擋得住**：構造一個 agent exit 0／末筆 JSONL 說 `done` 但分支無 commit／測試紅的 fixture → 結果驗證關必須判 `needs-fix`，不得放行為 `done`。
- **異質綁定生效**：dispatcher 若被要求把 reviewer 派給與 builder 同 vendor → 顯性拒絕或重派；blocking verdict 下 builder 側無法 override merge。
- **缺席不誤判**：reviewer 起不來 → `gate-absent`，deck/manifest 不顯示為健康通過。
- **fix-loop 有界**：注入連續失敗 → ≤2 輪後 `needs-human`，下游 `upstream-blocked`，非無限重試。
- **resume 不 restart**：reap 一個跑到一半的 task → 重派從 checkpoint 續，已過的關不重跑（以 manifest/journal 佐證）。
- **roster 可宣告**：`personas.yaml` 能宣告 role→vendor 且 dispatcher 據此起對應 CLI。

## 5. 相依與順序

```
P0-A 結果驗證完成關 ──┐
P0-B 異質+fail-closed gate ──┼─（三者同屬「完成正確性」核心，先做）
P0-C 有界 fix-loop ──┘
P1-A roster（依比較優勢） ──（與 P0-B 異質綁定一致，接著做）
P1-B resume 不 restart ──（獨立，長跑韌性）
P2 reap 歸屬 guardrail ──（獨立小改，隨時）
```

## 6. 合規（cortex policy：flat / shareable / 1.0.12）

- 分支 `feature/<slug>`；禁 commit main；每 code PR 附 `changelog.d/` 碎片 + `CHANGELOG.md [Unreleased]` 鏡像；PR title conventional-commit、body zh-tw + `Closes #N`。
- `tier: shareable`（R-21）：fixture／範例路徑中性佔位，無個人絕對路徑/機敏標記。
- behavior 變更同步 README/docs（R-18）；新增測試進 CI（R-19）。
- 本 spec 各項各自拆 issue + plan；建議 P0 三項作一個批次先走一輪完整 spec→plan→**異質 foreign-gate**→實作，順便當 cortex 自身採用該紀律的示範（吃自己的狗糧）。

## 7. 一句話

cortex 已經有分派系統的**骨**；hippo 這趟用血換到的是它的**紀律**——完成要靠結果驗證不是 exit code、審查要靠異質 vendor 且擋得住、卡關要誠實升級不硬推、撞 limit 要 resume 不 restart。把這四條落進 coordinator + persona，cortex 才從「會派工」變成「派得出可信結果」。
