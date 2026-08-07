---
status: accepted
work_item: cost-governance-cluster
---

# cost-governance-cluster Todo

成本治理／派工決策叢集的跨 issue 收斂紀錄（2026-07-27）。本 workstream 不對應單一 issue，而是 `#8` umbrella 底下叢集 A 的設計收斂與 `#208` 的拆分結果。

## Tasks

- [x] 派第一波 `#211`、`#214`、`#221`（互不碰檔，可並行）。（2026-07-27 完成）
- [x] `#210` 設計文件已落地（2026-08-07）：`docs/superpowers/plans/sizing-envelope-calibration.md`
      ＋`docs/superpowers/specs/sizing-envelope-calibration-{spec,design}.md`。**「零外部
      前置，可立即開始」的舊註記需更正**——設計層查證發現 `invariant_ceiling` estimator
      實際依賴一個尚未持久化的欄位（`invariant_count` 從未寫入 `CompletionRecord`，只在
      plan-review 當下一次性比對），需先補一張前置票才能開工；難度後驗 estimator 則需改用
      `merge_commit` 本地 diff 而非粒度不符的 `sizing_declaration_drift`。後續 estimator
      實作票的切分與依賴序見上述 design doc「建議後續實作票切分」。
- [ ] 決定是否採納 `fix-standard` combo（草稿見 `#202`）。
- [x] band 觸發加掛層的閾值：**預設 Yellow 起掛**（#221/PR #228 落地為具名常數可調）；repair 上限依 band 參數化 green=1/yellow=2（#218/PR #243），兩者已對齊。
- [ ] 確認次軸 `scope`、排除 agent-usage-stats 5 類、combo 為輸出而非輸入三項順推事項。
- [x] `claim.py` 熱點依序推進：實際落地序 `#211` → `#217` → `#213` → `#222` → `#223`（依賴邊 #213←#212 使 #217 前移，全程零衝突）。

## #208 收斂結果（2026-07-27）

`#208` 已關閉：13 張子單（`#211`–`#223`）＋接線收口 PR `#244` 全部 merged（PR
`#227`–`#244`，對照表與驗收走查見 `#208` 關閉 comment）。main 測試基線 1241 → 1567。
殘留事項明載於 `#208` close-out：縮小 canary 實測、`sizing_declaration_drift` 資料源、
Red→planner 拆分派工格式（需 `#209` 定案）、規則適用性逐項化（待 `#139`）、
TOCTOU 縫、StageExecutionKey 派工端消費、reviewer attestation 強度升級。


## 一句話狀態

`#208` 已完成拆分為 13 張可派工子單（`#211`–`#223`，全部 Green/Yellow）；`task_type` 主軸已定案；conventions v1.0.15 **已 merge，main 現在 1.0.15**（PR #225）。

---

## 相關 issue 全表

### 叢集 A — 派工決策／成本治理（共享定義，需一起 plan）

| Issue | 標題要旨 | 狀態 |
|---|---|---|
| `#139` | 共用基礎設施：task_type / log reader / 歸屬 / status view / ledger / session-health | **硬 blocker**；已定案為 taxonomy 所有者 |
| `#138` | 成本治理：meter → governance，cost-aware dispatch + 控速分流（judge 公式） | 設計文件已交付（`docs/superpowers/specs/cost-governance-judge-{spec,design}.md`），拆分後續實作票清單見 `docs/superpowers/plans/cost-governance-judge.md`；待 `#137`/`#209` 本體 code-landed 後可依序替換 interim stub |
| `#137` | one-shot 成效閉環：lesson-loop + 棘輪計分（track-record） | 設計文件已交付（`docs/superpowers/specs/oneshot-lesson-loop-{spec,design}.md`），待實作票落地 `track_record.py` 後可開 `openspec/changes/**` |
| `#136` | PreToolUse 容量閘門 hook（admission control） | open；「擋 vs 不擋」裁決已貼 |
| `#202` | feat(deck)：以 task_type 自動選擇 combo | open；已改為 additive with fallback，不再被 combo 缺口阻擋 |
| `#208` | sizing gate + lifecycle retry 成本治理 | open；已拆分 |
| `#209` | 模型能力封套、`capable()` 謂詞、topic×band 路由矩陣 | open |
| `#210` | 以自身 run 歷史校準 sizing 難度與能力封套 | open；**設計已落地**（`docs/superpowers/specs/sizing-envelope-calibration-{spec,design}.md`）；依賴 `#209` 欄位落地＋一張新前置票（`invariant_count` 持久化） |
| `#224` | subagent 交付失敗類型實測 | open（記錄性質） |
| `#8` | deck 自主派工閉環 umbrella | open；**已對帳確認不關**——六張子票全 open，另有整合層條件 |

### `#208` 拆分子單（13 張，全部可派工）

| Issue | 設計項 | 分數 | Band |
|---|---|---|---|
| `#211` | A.2 pre-claim readiness + 凍結集 | 5/10 | Yellow |
| `#212` | A.1 plan review gate 三項判定 | 4/10 | Yellow |
| `#213` | A.1 freeze point 位移 | 5/10 | Yellow |
| `#214` | B stage 級 execution key | 6/10 | Yellow |
| `#215` | C retry 分類骨架 | 5/10 | Yellow |
| `#216` | C 補齊分類與精準 invalidation | 5/10 | Yellow |
| `#217` | D source-owner 原子化 | 5/10 | Yellow |
| `#218` | E repair budget / circuit breaker | 4/10 | Yellow |
| `#219` | F reviewer input attestation | 5/10 | Yellow |
| `#220` | G final-before-merge | 4/10 | Yellow |
| `#221` | H.1 五維評分 | **3/10** | **Green** |
| `#222` | H.2 band 判定 + CompletionRecord | 4/10 | Yellow |
| `#223` | H.3 Red → needs_decomposition 路由 | 6/10 | Yellow |

**第一波可並行**：`#211`、`#214`、`#221`（互不碰檔）。

**檔案衝突熱點（必須序列）**
- `claim.py`：`#211` → `#213` → `#217` → `#222` → `#223`
- `planning.py`：`#212`、`#213`、`#221`
- `completion.py`：`#214`、`#215`、`#222`
- `delivery.py`：`#216`、`#218`、`#220`

### 叢集 B — lifecycle / claim 路徑（共享檔案，需序列，非共同設計）

`#203`（task intake 接入 WorkAuthority）、`#205`（per-work 模型鏈覆寫）、`#206`（durable GitHub provider authority 復發）

### 獨立（不需與上述一起 plan）

`#135`（persona enforcement 翻牌）、`#155`（Codex relay hook 路徑遷移，exit 127）、`#178`（teardown / 產物 GC）、`#204`（skill usage ledger）

### 本次關閉

- `#10` dispatch-discipline improve spec — P0-A/P0-B 已落地，P0-C 歸 `#208` 設計 E
- hippo `#63` — 全文併入 cortex `#208`

---

## 已定案

1. **`task_type` 主軸 = conventional-commit `type`**（feat/fix/docs/test/ci/refactor），機械判定，`#139` 為 taxonomy 所有者，與 `#202` 的循環等待解除。
2. **freeze point 移至 plan review 通過之後**（`#208` 設計 A.1）——原順序使「強審前移」自我矛盾。
3. **band 是 work item 屬性**，進 CompletionRecord，每次 repair 後重算（`#208` H.2）。
4. **Red → `needs_decomposition`** 自動回派 planner，拆分深度上限 2 層（`#208` H.3）。
5. **三種閘的邊界**：eligibility 擋（失敗終局）／admission 不擋只排隊（失敗可自癒）／routing 選資源。判準是「失敗是否可自癒」。
6. **#202 為 additive with fallback**：`ambiguous` fail closed；`absent` 與 `unparseable` **bypass** 落回明示路徑，且 bypass 須可觀測。`#8` 關閉條件已提修訂（原文把 `unknown` 與 `ambiguous` 並列 fail closed）。
7. **gate_spine 兩層制**：必要核心（`task_type` 決定，**只有這層計入** `acceptance_surfaces`）＋ band 觸發加掛層（不計入）。**`adversarial-review` 移入加掛層**。此為 `deck/schema.py` 的 schema 變更（`_COMBO_KEYS` 是嚴格白名單），非資料變更；已標註於 `#221`。

---

## 未決事項

1. **次軸 `scope`、排除 agent-usage-stats 5 類、combo 為輸出而非輸入** — 依提案順推，標為「如無異議即成立」，尚未逐項確認。
2. **`weight(work)` / `headroom(resource)` 是標量或向量** — `live_evidence` 類工作佔用序列埠、遠端建置主機等**獨佔型**資源，與 CPU load、quota 視窗屬不同稀缺維度。見 `#209` §9.5、`#136`。
3. **band 觸發加掛層的閾值** — Yellow 起掛或僅 Red 起掛；且需與設計 E 的 repair 上限對齊，不可各自為政。見 `#208`。
4. **`fix-standard` combo 是否採納** — 草稿已驗證可載入（見 `#202`），尚未落檔。

（原第 3 項「combo 覆蓋缺口」已有解方：#202 改為 additive with fallback，absent 走 bypass 不再阻擋；原第 4 項「#8 是否關閉」已對帳確認**不關**。）

---

## conventions v1.0.15（已完成）

PR **#225** 已 merge，`main` 現在是 1.0.15，分支已刪。CI 8/8 首跑全綠。

- 本地部署：`~/.local/share/paulsha-conventions`（1.0.15），`~/.agents/skills/preflight-ci` 現為受管 symlink；舊 skill 備份於 `~/.agents/preflight-ci.backup-20260727-134040`
- **preflight-ci 抓到兩個 CI 會擋的問題**（無 PR context 的 `policy_check --repo .` 抓不到）：
  - **R-09**：本 repo 用 `changelog.d/*.md` fragment，只改 `CHANGELOG.md [Unreleased]` 不算
  - **R-12**：feature 分支 pattern 是 `^feature/[a-z0-9][a-z0-9-]*$`，**不接受點號**（原名 `...-1.0.15` 被擋，改為 `...-1-0-15`）
  → 往後開 PR 前一律先跑 `preflight-ci`，帶完整 PR metadata。

---

## 關鍵事實（避免重犯）

- **registry 實際只有三個身分**，全走 executor `agy`：`claude-sonnet-4-6`（build/anthropic）、`gemini-3.6-flash-high`（review/google）、`Gemini 3.1 Pro (High)`（planning/google）。`codex/gpt-5.3-codex-spark`、`copilot/gpt-5.4`、`claude/sonnet`、`Luna(Opus max)` 均為**歷史紀錄，未登錄**。
- **只有一個 `build` capability 身分** → `#209` 的 routing 選擇語意目前是 no-op，優先序應向 eligibility（`#208`）傾斜。
- **`.paul-project.yml` 已於 policy v1.0.14 更名為 `.project-policy.yml`**。
- **既有 `coordinator/preflight.py` 是 build 期工具**，與 `#208` A.2 的 pre-claim readiness 語意不同，不可混用命名。
- **`MAX_FIX_ROUNDS = 2` 已存在**於 `delivery.py:36`，但只管 ship 階段 copilot loop。
- **phase 級 checkpoint 已存在**於 `manager_daemon.py` / `claim.py`。
- **cortex 對 `custom-skills/feature-delivery-pipeline` 為零 runtime 依賴**：`skill_ref` 在 `deck/schema.py` 僅驗證為非空字串，從未解析為實體 skill；全 repo 三處提及皆為註解。`tests/test_deck_data.py` 只斷言 card 存在於 catalog，不斷言 combo 使用它們。**cortex 流程可獨立演進，改 deck 無須動 SKILL.md。**（`cards.yaml:1` 與該測試註解仍寫著該 skill 是「真相源」，deck 分岔後會誤導，建議改為「初始轉錄來源（歷史）」。）
- deck 的 phase 共 7 個：`claim`（coordinator 記帳）+ `define`／`plan`／`build`／`verify`／`review`／`ship` 六大交付階段。新 combo 是在這六階段上取子集，不需發明新流程。
- `#136`/`#138`/`#139` 的跨 repo 搬遷編號殘留已修完，映射為 **舊 #138 → #137、舊 #140 → #138、舊 #142 → #139**。

---

## 建議下一步

1. 派第一波 `#211`、`#214`、`#221`（可並行）。
2. `#210` 的難度後驗 estimator——零外部前置，投報高。
3. 決定是否採納 `fix-standard` combo（草稿見 `#202`，已驗證可載入）。
4. 決定 band 觸發加掛層的閾值（Yellow 起掛或僅 Red），並與設計 E 的 repair 上限對齊。

---

## 本 session 的 issue 動作總表

供下一位接手者查證來源；每一項都可在對應 issue 的 comment 找到完整依據。

| Issue | 動作 |
|---|---|
| hippo `#63` | 全文併入 cortex `#208` 後**關閉**；hippo 側殘留（#41 拆 4 張）已於關閉 comment 註明 |
| `#208` | 併入 #63；新增設計 **A.1**（freeze 位移）、**A.2**（preflight 排序／凍結集／終局-可重試分類）、**H.1**（三維機械算＋二維宣告）、**H.2**（band 進 CompletionRecord）、**H.3**（Red→needs_decomposition）；實作註記（E 擴展既有 `MAX_FIX_ROUNDS`、B 建立在 phase checkpoint 上）；拆分索引表；**gate_spine 兩層制** |
| `#209` | **新開**——能力封套、`capable()` 六項謂詞、topic×band 路由矩陣、`resource-inventory.yaml` 四欄位；§9.5 admission 邊界；**§4 更正**（原列 roster 未登錄於 registry） |
| `#210` | **新開**——以自身 run 歷史校準 sizing 與封套；**範圍更正**（初版誤寫成 patchmud 雙向橋接，已改為 cortex-only、零外部依賴） |
| `#224` | **新開**——subagent 交付失敗類型實測 + 六項機械驗收 |
| `#211`–`#223` | **新開 13 張**——#208 的拆分子單，全部 Green/Yellow |
| `#221` | 補充：gate_spine 加掛層是 `deck/schema.py` schema 變更，非改 YAML |
| `#136` | 「擋 vs 不擋」裁決（判準＝失敗是否可自癒）、閘序、`admit()` 重量判準、範圍收窄至 ad-hoc 破口；編號殘留修正 |
| `#138` | 定向說明（judge 四因子補齊狀態）；編號殘留修正（自我引用 → #137） |
| `#139` | 欄位契約說明；`task_type` 主軸**提案**（經校訂）與**決策定案**；編號殘留修正（7 處） |
| `#137` | track-record 資料來源說明＋**更正**（patchmud 不作為資料來源） |
| `#202` | additive with fallback（absent/ambiguous 區分）、bypass 可觀測性、`fix-standard` 草稿、設計自由度確認 |
| `#8` | 關閉條件修訂提案（`unknown` 拆出 fail closed）；逐項對帳後**明載不關** |
| `#10` | 逐項對帳後**關閉**（P0-A/P0-B 已落地，P0-C 歸 #208 設計 E） |
| PR `#225` | conventions v1.0.15 升級，CI 8/8 綠，**已 merge**，分支已刪 |

### 本 session 未動的 issue

`#135`、`#155`、`#178`、`#203`、`#204`、`#205`、`#206`

---
