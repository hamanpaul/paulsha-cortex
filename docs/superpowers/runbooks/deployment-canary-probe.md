# Deployment canary probe repository

`.github/workflows/deployment-canary.yml` 在一次性容器內對一個 private probe repository
執行真實的 `cortex run work intake --combo feature-oneshot`，走完 plan → build → verify →
review → ship，**真的開 PR 並 merge**。probe repository 因此是消耗品：每次 canary 用一個
全新、由範本產生的 repo。範本在 `qualification/probe-template/`。

本文件描述 probe repository 的準備與重置，以及 driver 在容器內替 probe 做的事；canary 的
驗收位置見 `docs/superpowers/runbooks/trust-root-transactional-install.md` §7 與
`docs/superpowers/runbooks/release-qualification.md`。

## 1. 範本內容與理由

| 檔案 | 為什麼需要 |
| --- | --- |
| `.cortex/work-items.yaml` | intake 只接受 Monitor snapshot 已有的 `(repo, work_id)`，且 `--issue N` 必須已在 `mapped_issues`。這裡宣告 work item `deployment-canary-probe`、連結 OpenSpec change；issue link 由 operator 每次補上（§3）。 |
| `docs/superpowers/workstreams/deployment-canary-probe/todo.md` | work item 需要一份 confirmed todo（frontmatter `work_item`）；它也是 kind=plan 的 accepted planning 產物，ship 時要求它在 default branch 上全部勾選。frontmatter 刻意不帶 `domain_breadth`／`state_consistency`，否則 sizing 會把這個小任務判成 Red 而要求拆解。 |
| `docs/superpowers/specs/deployment-canary-probe-spec.md`、`-design.md` | 與 todo 一起湊齊 spec／design／plan 三種 accepted planning 產物，`assess_planning_completeness` 成立，planning 以 deterministic 路徑通過、不跑 brainstorm。容器內 `cortex-reviewer-planner` 只有 agy 與 copilot 憑證，湊不出兩個不同 independence domain 的 planner；brainstorm 一跑就會停在 `no-heterogeneous-planner`。`-design.md` 也滿足 writing-plans 卡的 `requires`。 |
| `openspec/changes/deployment-canary-probe/`（proposal、tasks、spec delta） | feature-oneshot 的 ship 有 openspec-archive 卡，沒有 mapped change 就無法 archive。archive gate 要求 tasks 全勾、`openspec validate --strict` 通過、CHANGELOG 提到 change id 或有 `changelog.d/<change>.md`。 |
| `.project-policy.yml` | ship 的 preflight 由 `PSC_PREFLIGHT_CMD` 交給治理引擎 `policy_check.preflight --offline`，它讀這份檔：`preflight.steps` 只接受 `validation`／`tests` 兩種 kind 且至少要有一步；`conventions_engine.mode: pip` 讓引擎直接採用已安裝的 `policy-check`（版本必須逐字等於 `policy_version`），不需要另放 `policy-check.yml`；`tier: shareable`、`agent_files.mode: symlink`。 |
| `CLAUDE.md`（含 `policy_version`）與 `AGENTS.md`／`GEMINI.md`／`.github/copilot-instructions.md` symlink | policy R-13／R-14。 |
| `README.md`（Install／Usage／Version 段）、`VERSION`、`CHANGELOG.md`（`## [Unreleased]`） | policy R-02～R-07；archive gate 也讀 CHANGELOG。 |
| `.gitignore` | compileall／pytest 會產生 `__pycache__`／`.pytest_cache`；preflight 前後工作樹必須乾淨，否則判為 tree-race。 |
| `.github/workflows/probe-checks.yml` | merge 條件是 PR 的 check-runs 非空且全綠；也滿足 R-19（有 `tests/` 就要有跑測試的 CI）。 |
| `src/canary_probe.py`、`tests/test_canary_probe.py` | 任務本身。每張卡的 gate 都以 gate 身分跑 `python3 -m pytest -q`（`PSC_GATE_CMD_PYTEST`，部署層固定值），因此 base 版本的測試就必須可收集且全綠；測試用 `unittest` 撰寫，pytest 與 preflight 的 `unittest discover` 都收得到。 |

不需要預放 `docs/superpowers/plans/*.md`：planning 完整時，plan 卡會把 accepted 的
kind=plan（todo）內容落到 build 卡要求的 `docs/superpowers/plans/*deployment-canary-probe*.md`。

canary 任務：`normalize_label()` 對全空白標籤回傳 `"unnamed"`，並補回歸測試、README、
`changelog.d/deployment-canary-probe.md`、`CHANGELOG.md [Unreleased]`，勾完 todo 與
OpenSpec tasks。

範本已以 `policy-check` 1.0.17 與 `openspec` 1.10.0 本機模擬過：未修改的範本 bare
`python3 -m policy_check --repo .` 全數 pass；照上述任務修改並 `openspec archive` 之後，以
ship 實際送出的 PR 上下文（title `feat(workflow): 完成 deployment-canary-probe`、label
`enhancement`、body 含 `Closes #N`）跑 `policy_check.preflight --offline` 為
`PREFLIGHT PASS`，且在 reference image 內重跑結果相同。

## 2. 建立 private template repository（一次）

以下 `OWNER` 是放 probe 的 GitHub 帳號或組織；指令不需任何 secret 值。

```sh
cortex_checkout="path/to/paulsha-cortex"          # 本 repo 的 checkout
template_repo="OWNER/cortex-canary-probe-template"
seed="$(mktemp -d)"
cp -a "$cortex_checkout/qualification/probe-template/." "$seed/"
git -C "$seed" init --initial-branch=main
git -C "$seed" add -A
git -C "$seed" commit -m "chore: seed deployment canary probe template"
gh repo create "$template_repo" --private --source "$seed" --push
gh repo edit "$template_repo" --template
```

`cp -a` 保留三個 agent 檔的 symlink；不要用會把 symlink 展開成複本的方式複製。

## 3. 每次 canary 前：建立 probe target

1. 從範本建立新的 private repo（default branch `main`）：

   ```sh
   probe_repo="OWNER/cortex-canary-probe-$(date +%Y%m%d%H%M)"
   gh repo create "$probe_repo" --private --template OWNER/cortex-canary-probe-template
   ```

   repo 名稱會成為容器內 Manager 來源樹下的 checkout 目錄名，不可與已安裝的來源 repo
   （`paulsha-cortex`）同名。

2. repo 設定（driver 在 intake 前會以 Manager 帳號逐項預檢，見 §4）：
   - 開啟 Issues 與 Actions；允許 merge commit（Cortex 以 `--merge --match-head-commit`
     合併）。
   - branch rule／ruleset 可要求 `tests` check，但**不要**要求 approving review——Cortex
     不讀 branch protection，只看 check-runs 與 mergeable 狀態，approving review 規則會讓
     merge 卡住。
   - 建立 ship 會掛上 PR 的 label（由 production PR metadata 導出，目前只有
     `enhancement`）：`gh label create enhancement --repo "$probe_repo" --force`。
   - **Copilot code review**：closeout 要求 `copilot` 與 `maintainer-review` 兩種 review
     gate 恰好一種；maintainer-review 需要 operator 在容器內 attest，canary 走不到，因此
     Manager 的 GitHub 帳號必須能對這個 private repo 請求 Copilot code review（帳號要有含
     code review 的 Copilot 權益，且 repo owner 未停用它）。GitHub 沒有能在開 PR 前確認這件
     事的公開 API；第一次使用某個 owner 前，請用 Manager 帳號在範本 repo 開一個拋棄式 PR，
     以 `gh api --method POST repos/OWNER/REPO/pulls/N/requested_reviewers -f
     'reviewers[]=copilot-pull-request-reviewer[bot]'` 請求一次並確認 Copilot 留下 review，
     再關掉該 PR。

3. 建立 probe issue：

   ```sh
   gh issue create --repo "$probe_repo" \
     --title "deployment canary: blank labels become unnamed" \
     --body "normalize_label(\"   \") 回傳 \"unnamed\"；補回歸測試、README、changelog.d/deployment-canary-probe.md 與 CHANGELOG [Unreleased]；勾完 todo 與 OpenSpec tasks；通過 .project-policy.yml 的 preflight。"
   ```

4. 在該 repo 的 `main` 把 issue link 加進 `.cortex/work-items.yaml`（ref 必須是
   `OWNER/REPO#N`，大小寫與步驟 5 的 `CORTEX_RC_PROBE_REPOSITORY` 完全一致），commit 並
   push；確認 `main` 的 `tests` check 為綠：

   ```yaml
   version: 1
   work_items:
     deployment-canary-probe:
       title: "Deployment canary probe task"
       links:
         - kind: github_issue
           ref: "OWNER/REPO#N"
         - kind: openspec
           ref: "deployment-canary-probe"
       excludes: []
   ```

   intake 雖會寫入 issue link，但同一次呼叫不會重新採信；link 必須在 driver clone 之前就在
   `main` 上。

5. 在 `paulsha-cortex` 的 protected environment `rc-qualification` 設定 variables：

   ```sh
   gh variable set CORTEX_RC_PROBE_REPOSITORY --env rc-qualification --body "$probe_repo"
   gh variable set CORTEX_RC_PROBE_WORK_ID --env rc-qualification --body deployment-canary-probe
   gh variable set CORTEX_RC_PROBE_ISSUE --env rc-qualification --body "<issue number>"
   ```

6. 同一 environment 的 secrets（以 `gh secret set <NAME> --env rc-qualification < <file>` 從
   檔案讀入，值不要出現在命令列）。形狀由 installer 的 credential adapter 驗證，內容不進
   evidence：

   | secret | 內容 |
   | --- | --- |
   | `CORTEX_RC_CODEX_AUTH` | builder 用 codex 登入後的 `auth.json`（canary builder 為 codex） |
   | `CORTEX_RC_AGY_AUTH` | agy 1.2.11 登入後的 `~/.gemini/antigravity-cli/antigravity-oauth-token` 原檔 |
   | `CORTEX_RC_COPILOT_AUTH` | copilot 1.0.88 登入後的 `~/.copilot/config.json`，至少含非空 `copilotTokens`；建議只保留 `copilotTokens`／`loggedInUsers`／`lastLoggedInUser` |
   | `CORTEX_RC_MANAGER_GITHUB_AUTH` | Manager 用 gh 的 `hosts.yml`（需能 clone、push、開 PR、merge、請求 Copilot review） |
   | `CORTEX_RC_BUILDER_AGY_AUTH` | 只有 install plan 要求 `(builder, agy)` 時才需要；預設 canary config 不需要 |

   舊格式的 gemini-cli `oauth_creds.json` 與 `~/.config/github-copilot/hosts.json` 對現行
   CLI 無效，adapter 會以檔名 allowlist 或形狀檢查拒絕。

## 4. 容器內 driver 在 intake 前做的事

operator 不需要、也不應該手動 clone probe 或改 Monitor／coordinator 狀態。Manager GitHub
dry-run 通過後，`qualification/driver.py` 依序：

1. **遠端預檢**（Manager 身分、`gh api`，只讀）：repo 可讀且 Manager 有 push 權限、未
   archived、default branch 為 `main`、Issues 開啟、允許 merge commit；issue 存在且為
   open；ship 的 PR label 已存在；ruleset／branch protection 沒有要求 approving review
   （讀不到 classic protection 時不視為違規）。任一不成立即以列出的原因失敗，不進件。
2. **本機工具預檢**：gate 身分能以系統層 `python3` 跑釘版本的 pytest；部署 venv 與系統層
   `python3` 都裝有與契約相同版本的 `policy-check`；Manager 的 `PATH` 上有
   universal-ctags。
3. **clone 與登記**：以 Manager 身分、經其 root-owned gitconfig 的 gh credential helper 與
   egress proxy，把 probe clone 到已安裝 plan 的 `repo-source-tree`（`<state>/repos/<repo
   名>`，與 installer 放受治理 repo 的同一個 Manager-owned 容器，job 帳號的唯讀 default ACL
   自動繼承）；設定 checkout 的本地 `user.name`／`user.email`（契約的 canary 身分，
   `.invalid` 保留網域）——容器 hostname 沒有網域，git 自動推導的 email 會拒絕 commit，而
   per-job clone 與 Manager 的 archive commit 都從來源 checkout 取得 identity。接著在
   `PSC_PROJECT_CONFIG_ROOT` 寫入 root-owned 的 `project-cortex.yaml`（一列
   `exact_project: true` workspace，形狀與 `cortex install service` 寫的相同），以 installed
   runtime 驗證 Monitor 的 `load_config()` 讀得到它、Manager 的
   `resolve_trusted_repo_root()` 會把 `OWNER/REPO` 恰好解析到這份 checkout，最後重啟
   Monitor 讓它重讀設定。已有 `project-cortex.yaml` 或同名 checkout 時直接拒絕，不合併。
4. **版本對齊**：probe 的 `policy_version` 必須等於已安裝的 `policy-check`。
5. **等 authority**：以 intake 自己用的 `load_work_authority()` 每 15 秒檢查一次，直到
   Monitor snapshot（含 GitHub provider 的 durable 狀態）產生 confirmed authority 且
   `mapped_issues` 含 `N`，最多 900 秒；逾時訊息帶最後一次的原因（例如 issue link 不在
   `main`）。

之後才執行 `cortex run work intake`。dispatch 若停在 needs_human／failed，driver 的失敗訊息
會帶出 `cortex work show --json` 的 `blocking_reason`（例如 Copilot review 沒有送達時的
review gate 原因）。

## 5. reference image 與部署 venv 提供的釘版本工具

單一真相是 `qualification/contract.py`，workflow、Dockerfile 與 driver 由它導出或以測試
逐字核對：

| 工具 | 誰用 | 來源與釘法 |
| --- | --- | --- |
| pytest 7.4.4（`python3-pytest` 與 `python3-pluggy`／`python3-iniconfig`／`python3-packaging`） | gate 身分執行 `PSC_GATE_CMD_PYTEST`，解析到系統層 `python3`（permgen `SYSTEM_PYTHON_DISTRIBUTIONS`） | Dockerfile 以 `apt-get install 名稱=版本` 安裝 Ubuntu 24.04 release pocket 的固定版本 |
| universal-ctags 5.9 | 引擎帶 PR 上下文時 R-22 的 symbol 分析；缺它 policy 階段直接 RuntimeError（conventions runtime bundle 的 prerequisites 也列它） | 同上，apt 釘版本 |
| `policy-check` 1.0.17 | `PSC_PREFLIGHT_CMD` 的 backend（部署 venv），以及 Manager 以相對名 `python3 -m policy_check` 跑的 preflight policy stage 與 archive gate（系統層） | workflow 下載 paulsha-conventions v1.0.17 release 的 cp312 runtime bundle，驗 archive sha256 後取出 wheel 再驗 wheel sha256，放進 wheelhouse；bundle 把它裝進部署 venv（與 production runbook 裝進部署 venv 的決定一致），run.sh 的系統層安裝同時涵蓋相對名 `python3` |

`policy-check` 採「提供已安裝的引擎」而不是讓 probe 走豁免：production 部署同樣把
`policy-check==<policy_version>` 裝進部署 venv，而 `PSC_PREFLIGHT_CMD` 沒有受支援的跳過
引擎路徑。升級引擎時要同時改 `WHEELS["policy_check"]` 與範本的 `policy_version`（測試
會擋下兩者不一致）。

## 6. 執行與確認

```sh
gh workflow run deployment-canary.yml --repo OWNER/paulsha-cortex --ref <candidate branch>
```

canary 以該 ref 的 HEAD 作為 candidate SHA。完成後分別確認：

- artifact `deployment-canary-<sha>` 內的 `qualification.json` 為 passed；
- probe repo 出現已 merge 的 PR（分支 `feature/<issue>-deployment-canary-probe`，body 帶
  `Closes #N`），issue 已關閉，OpenSpec change 已 archive，todo 全勾。

## 7. 每次 canary 後：重置

PR 已 merge、issue 已關、change 已 archive、todo 已勾滿，同一個 target 不能重跑。下一次
一律回到 §3 從**未修改的範本**建立新的 repo、新 issue，並更新三個 variables。舊 target
依保留政策 archive（`gh repo archive "$probe_repo"`）；本文件不刪除 repository。範本本身
只在 `qualification/probe-template/` 改版時才以 §2 重新推送。

## 8. 仍待 live run 證實的部分

先前讀碼列出的阻斷（容器內沒有 probe checkout、沒有 pytest、沒有 policy-check）已由 §4、
§5 解決。以下仍只能由第一次成功的 live canary 證實：

- **Copilot code review 權益**：沒有開 PR 前可查的公開 API，只能依 §3 步驟 2 由 operator
  先行確認；不成立時 review gate 逾時轉 needs_human，driver 會帶出原因。
- **provider 與 GitHub 的實際行為**：provider smoke、planning／build／review 的模型輸出、
  GitHub provider refresh 與 Copilot review 的送達時間，都要靠真實憑證跑過一次；本機只驗證
  了 image 建置、安裝流程、probe 範本在引擎與 openspec 下的結果，以及 driver 的單元測試。

deck compile 在 workflow 路徑讀的是 `PSC_REPO_ROOT`（`paulsha-cortex`）的
`.project-policy.yml`，但**不是缺陷**：那份讀取只用來產生 slice 文件的 verification 骨架，
workflow 路徑的 `default_workflow_manifest()` 只取 `WorkflowManifest`，其中不帶任何
verification 內容；workflow 各卡的 gate 命令來自部署層的 `PSC_GATE_CMD_*`，ship preflight
則由引擎在目標 checkout 內讀 probe 自己的 `.project-policy.yml`。
