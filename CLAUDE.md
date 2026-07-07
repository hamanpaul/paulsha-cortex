<!-- managed-by: hamanpaul/paulsha-conventions@v1.0.7 -->
<!-- 此為 canonical 真檔；AGENTS.md / GEMINI.md / .github/copilot-instructions.md 為指向本檔的 symlink，只維護本檔 -->
policy_version: 1.0.7

# Agent Policy Checklist

本 repo 受 hamanpaul project policy v1.0.7 管轄。
所有 agent 進入 session 時，必須依下列 checklist 行動。

## 本 repo 的 profile
- policy_profile: `flat` （見 `.paul-project.yml`）
- policy_version: `1.0.7`

## 動工前
- [ ] 確認當前分支不是 `main`
  - 若在 `main`，先開 `feature/<slug>` 分支
  - 若在 `feature/*`，可直接工作，或再開 `wt/<feature>/<subtask>`
- [ ] 若本任務跨多個子項，先建議用 `git worktree` 拆開

## 改 code 時
- [ ] 同一 PR 必須同步更新 `CHANGELOG.md [Unreleased]`
- [ ] 除非可明確標示為 docs-only / test-only / chore，否則不得省略 CHANGELOG
- [ ] code_paths 涵蓋的檔案變動皆視為 code change

## 改版號時（release 觸發時）
- [ ] 嚴格遵循 `<MAJOR>.<MINOR>.<PATCH>[-fix.N]`
- [ ] PATCH bump 對應 profile：
  - `stage-driven`: 一個 stage 落地
  - `flat`: 一個 feature batch 完成
- [ ] MINOR bump 需滿足：feature 群組全 landed + 7 天無 hotfix
- [ ] MAJOR bump 需使用者明確核可

## 完成任務（claim done）前
- [ ] `CHANGELOG.md [Unreleased]` 有對應 entry（或 PR 標 `skip-changelog` + 理由）
- [ ] `VERSION` 內容與意圖一致（release label PR 才可偏離 latest tag）
- [ ] `.github/pull_request_template.md` checklist 全勾
- [ ] `python3 -m policy_check --repo .` 無任何 failure
- [ ] 若本 repo 有額外測試 / lint / build 指令，需一併通過
- [ ] 若跳過任何檢查，PR 必須帶對應豁免 label + 理由

## 禁止
- 直接 commit 到 `main`
- 建立不符合命名規則的分支（必須 `feature/<slug>` 或 `wt/<feature>/<subtask>`）
- 發明新 `policy-exempt:*` label（**只能用 policy 列舉的白名單**）
- 把 agent symlink（AGENTS.md / GEMINI.md / .github/copilot-instructions.md）還原成獨立複本（`agent_files.mode: symlink` 下 R-14 會 FAIL）

## Exemption Labels 白名單
僅允許使用以下 labels 豁免對應規則（其他一律視同未豁免）：
- `policy-exempt:readme-sections` — R-02 README 必備段落
- `policy-exempt:changelog-format` — R-04 CHANGELOG 格式
- `policy-exempt:pr-title` — R-10 PR title conventional-commit 格式
- `policy-exempt:branch-name` — R-12 分支來源規則
- `policy-exempt:agent-files` — R-13 agent convention files 存在
- `policy-exempt:cli-help` — R-16 CLI help 同步
- `skip-changelog` — R-09 code 變動要求 CHANGELOG entry（特殊用途，需附理由）
- `wip` — R-11 自動通過 PR body checkbox 未全勾（work in progress）

> 各 policy 版本新增的 exemption label 列於下方對應「新增規則」段。

## v1.0.1 新增規則（issue 連結 / docs 對齊 / 語言）
> 本段於 policy 1.0.1 隨 R-17 / R-18 與語言規範新增。

- **R-17（PR↔issue，FAIL gate）**：PR body 引用 issue（`#N`）時必須為 closing-keyword 形式（`Closes` / `Fixes` / `Resolves #N`），merge 由 GitHub 原生自動關閉 issue 並留下 cross-reference；只引用不關閉時上 `policy-exempt:issue-link`。
- **R-18（docs 對齊，WARN，不擋 merge）**：`code_paths` 有變動但 `README.md` / `docs/**` 未同步時提醒；純內部變動可上 `policy-exempt:docs-sync`。
- **語言規範（checklist）**：依 repo 來源決定語言——`github.com/hamanpaul/*`、`github.com/paulc-arc/*` → zh-tw；arcadyan GitLab → en_US。涵蓋 PR 標題／內文與所有 comment。本 repo 屬 `hamanpaul` → zh-tw。
- **動工前（軟性，不打斷流程）**：若任務對應某 issue，`gh issue view <N>` 核對相關性後分支可命名 `feature/<N>-<slug>`，開 PR 於 body 寫 `Closes #N`；查無對應 issue 照常進行，不另開、不停。
- **Exemption 白名單新增**：`policy-exempt:issue-link`（R-17）、`policy-exempt:docs-sync`（R-18）。

## v1.0.2 新增規則（CI 測試 / 版本同步）
> 本段於 policy 1.0.2 隨 R-19 / R-20 新增。

- **R-19（CI 必須跑測試，FAIL gate）**：repo 存在 `tests/`（含 `test_*.py` / `*_test.py`）時，`.github/workflows/**` 必須有至少一個 workflow 實際執行測試（pytest / unittest / npm test 等）；新增測試套件而 CI 未涵蓋時須同步補上；豁免 label `policy-exempt:ci-tests`。本 template 已內建 `tests.yml` 骨架（tests/ 出現即自動執行 pytest）。
- **R-20（workflow policy_version 同步，FAIL gate，無豁免 label，比照 R-14）**：workflow 內宣告的 `policy_version` / `POLICY_VERSION` semver 字面值必須與 `.paul-project.yml` 的 `policy_version` 一致；input 宣告與 `${{ inputs.* }}` 模板表達式不在檢查範圍。
- **Exemption 白名單新增**：`policy-exempt:ci-tests`（R-19）。R-20 不設豁免。

## v1.0.3–1.0.4 新增規則（機密掃描 tier）
> 本段於 policy 1.0.3 隨 R-21 與 `tier` 欄位新增，1.0.4 將標記 config 化。

- **R-21（機密掃描，FAIL gate）**：宣告 `tier: shareable` 的 repo 含雇主機敏標記（內部代號、裝置型號、build 主機等）、個人絕對路徑或憑證模式則 FAIL；`tier: work` / `personal` 視為 not-applicable；合法引用上 `.paul-project.yml` 的 `secret_scan.allow`，命中時上 `policy-exempt:secret-scan` 並附理由。1.0.4 將機敏標記改為 baseline 資料檔 + per-repo extend-only 疊加。
- **`.paul-project.yml` 新增**：`tier`（`shareable` / `work` / `personal`）與 `secret_scan.allow` / `secret_scan.markers` / `secret_scan.public_names`。
- **Exemption 白名單新增**：`policy-exempt:secret-scan`（R-21）。

## v1.0.5 新增規則（doc-alignment 三層治理）
> 本段於 policy 1.0.5 隨 R-22 新增。

- **R-22（doc 懸空引用，FAIL / WARN）**：搬移／改名／刪除 code 產物（檔案、`def` / `class`）後，`README.md` / `docs/**` 仍殘留引用時偵測——本次變更新破壞 **FAIL**、陳年懸空 **WARN**、無 diff context（本地）降 WARN；無法即時處理上 `policy-exempt:doc-reference` 並附理由。
- **Doc-alignment review（advisory）**：除 R-22 抓得到的結構性懸空，另留意語意陳舊（引用都還在、但 docs 描述了已被改掉的架構／行為）；發現時於 PR 留言建議作者更新。不擋 merge；建議將 GitHub Copilot 設為 PR reviewer 啟用此層。
- **`.paul-project.yml` 新增**：`doc_reference.allow`。
- **Exemption 白名單新增**：`policy-exempt:doc-reference`（R-22）。

## v1.0.6 新增規則（agent 慣例檔 symlink 單一真檔 / 引擎 pin attestation）
> 本段於 policy 1.0.6 隨 R-23 與 agent 慣例檔單一真檔模型新增。

- **agent 慣例檔單一真檔（R-13 / R-14 升級）**：canonical `CLAUDE.md` 為唯一真檔，`AGENTS.md` / `GEMINI.md` / `.github/copilot-instructions.md` 改為指向它的 symlink，只維護一份，消除四份 byte-identical 複本。`.paul-project.yml` 設 `agent_files.mode: symlink`（預設 `copy` 向後相容，下游可漸進遷移）；`symlink` 模式下 R-14 強制鏡像檔為 resolve 到 `CLAUDE.md` 的 symlink，divergent 複本／錯誤目標／canonical 為 symlink 皆 FAIL。
- **R-23（引擎 pin ⟷ policy_version attestation，FAIL gate）**：workflow `uses:` 指向 `conventions_engine.repo` 的引擎版本（tag `@vX.Y.Z`，或 SHA `@<sha>` + 尾註 `# vX.Y.Z`）必須與 `.paul-project.yml` 的 `policy_version` 一致，否則 FAIL；純 SHA 無註解 WARN；`./` 在地引用或未設 `conventions_engine.repo` 則 not-applicable。閉合「repo 實際 pin 的引擎版本 ⟷ 宣告 policy_version」這條既有引擎只驗 intra-repo、看不到的缺口。
- **`.paul-project.yml` 新增**：`agent_files.mode`（`copy` / `symlink`）、`conventions_engine.repo`（`owner/repo`，空字串為 NA sentinel）。
- **Exemption 白名單新增**：`policy-exempt:engine-pin`（R-23）。
- **禁止新增**：把 agent symlink 還原成獨立複本（`agent_files.mode: symlink` 下 R-14 會 FAIL）。

## v1.0.7 新增規則（MOC 對齊）
> 本段於 policy 1.0.7 隨 R-24 新增。

- **R-24（moc-alignment，opt-in，FAIL/WARN）**：repo 於 `.paul-project.yml` 宣告 `moc`（`static` / `map` / `triggers`）後生效（未宣告 → not-applicable）。三瓣：靜態鮮度（`moc.triggers` 命中但 `moc.static` 未同步 → WARN）／動態連結懸空（`moc.map` 連到不存在的受治理產物 `openspec/changes/**`・`docs/superpowers/{specs,plans}/**`，本次新破壞 FAIL、陳年 WARN）／動態連結孤兒（active openspec change・plan・spec 未被連結 → WARN，永不 FAIL）。內容跟著專案走（留各專案 repo），引擎只出規則；platform-agnostic（純 git-level，不依賴 GitHub/GitLab）。
- **MOC 狀態語意對齊（advisory）**：`moc.map` 上某 stage 宣稱 done 是否真 done、被 postpone 的 stage 是否還誤掛 done，屬語意層，由 Copilot reviewer 留言提醒，不入確定性規則。
- **Exemption 白名單新增**：`policy-exempt:moc-alignment`（R-24）。
