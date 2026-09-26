# Deployment canary probe repository

`.github/workflows/deployment-canary.yml` 在一次性容器內對一個 private probe repository
執行真實的 `cortex run work intake --combo feature-oneshot`，走完 plan → build → verify →
review → ship，**真的開 PR 並 merge**。probe repository 因此是消耗品：每次 canary 用一個
全新、由範本產生的 repo。範本在 `qualification/probe-template/`。

本文件只描述 probe repository 的準備與重置；canary 的驗收位置見
`docs/superpowers/runbooks/trust-root-transactional-install.md` §7 與
`docs/superpowers/runbooks/release-qualification.md`。

## 1. 範本內容與理由

| 檔案 | 為什麼需要 |
| --- | --- |
| `.cortex/work-items.yaml` | intake 只接受 Monitor snapshot 已有的 `(repo, work_id)`，且 `--issue N` 必須已在 `mapped_issues`。這裡宣告 work item `deployment-canary-probe`、連結 OpenSpec change；issue link 由 operator 每次補上（§3）。 |
| `docs/superpowers/workstreams/deployment-canary-probe/todo.md` | work item 需要一份 confirmed todo（frontmatter `work_item`）；它也是 kind=plan 的 accepted planning 產物，ship 時要求它在 default branch 上全部勾選。frontmatter 刻意不帶 `domain_breadth`／`state_consistency`，否則 sizing 會把這個小任務判成 Red 而要求拆解。 |
| `docs/superpowers/specs/deployment-canary-probe-spec.md`、`-design.md` | 與 todo 一起湊齊 spec／design／plan 三種 accepted planning 產物，`assess_planning_completeness` 成立，planning 以 deterministic 路徑通過、不跑 brainstorm。容器內 `cortex-reviewer-planner` 只有 agy 與 copilot 憑證，湊不出兩個不同 independence domain 的 planner；brainstorm 一跑就會停在 `no-heterogeneous-planner`。`-design.md` 也滿足 writing-plans 卡的 `requires`。 |
| `openspec/changes/deployment-canary-probe/`（proposal、tasks、spec delta） | feature-oneshot 的 ship 有 openspec-archive 卡，沒有 mapped change 就無法 archive。archive gate 要求 tasks 全勾、`openspec validate --strict` 通過、CHANGELOG 提到 change id 或有 `changelog.d/<change>.md`。 |
| `.project-policy.yml` | ship 的 policy／preflight gate 讀它。`preflight.steps` 帶 `validation` 與 `tests` 兩種 kind（deck compile 只認這兩種，缺少時產生必定失敗的佔位指令）；`tier: shareable`、`agent_files.mode: symlink`，`conventions_engine.mode: pip` 讓引擎 preflight 不必另放 `policy-check.yml`。 |
| `CLAUDE.md`（含 `policy_version`）與 `AGENTS.md`／`GEMINI.md`／`.github/copilot-instructions.md` symlink | policy R-13／R-14。 |
| `README.md`（Install／Usage／Version 段）、`VERSION`、`CHANGELOG.md`（`## [Unreleased]`） | policy R-02～R-07；archive gate 也讀 CHANGELOG。 |
| `.gitignore` | compileall／pytest 會產生 `__pycache__`；preflight 前後工作樹必須乾淨，否則判為 tree-race。 |
| `.github/workflows/probe-checks.yml` | merge 條件是 PR 的 check-runs 非空且全綠；也滿足 R-19（有 `tests/` 就要有跑測試的 CI）。 |
| `src/canary_probe.py`、`tests/test_canary_probe.py` | 任務本身。每張卡的 gate 都跑 `python3 -m pytest -q`，因此 base 版本的測試就必須可收集且全綠。 |

不需要預放 `docs/superpowers/plans/*.md`：planning 完整時，plan 卡會把 accepted 的
kind=plan（todo）內容落到 build 卡要求的 `docs/superpowers/plans/*deployment-canary-probe*.md`。

canary 任務：`normalize_label()` 對全空白標籤回傳 `"unnamed"`，並補回歸測試、README、
`changelog.d/deployment-canary-probe.md`、`CHANGELOG.md [Unreleased]`，勾完 todo 與
OpenSpec tasks。

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

2. repo 設定：開啟 Issues 與 Actions、允許 merge commit（Cortex 以 `--merge
   --match-head-commit` 合併）。branch rule 可要求 `tests` check，但**不要**要求 approving
   review——Cortex 不讀 branch protection，只看 check-runs 與 mergeable 狀態，approving
   review 規則會讓 merge 卡住。Manager 的 GitHub 帳號必須能對此 repo 請求 Copilot code
   review（closeout 要求 `copilot` 與 `maintainer-review` 兩種 gate 恰好一種，而
   maintainer-review 需要人在容器內 attest）。
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

   intake 雖會寫入 issue link，但同一次呼叫不會重新採信；link 必須在 Monitor 掃描前就在
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
   | `CORTEX_RC_MANAGER_GITHUB_AUTH` | Manager 用 gh 的 `hosts.yml`（需能 push、開 PR、merge、請求 Copilot review） |
   | `CORTEX_RC_BUILDER_AGY_AUTH` | 只有 install plan 要求 `(builder, agy)` 時才需要；預設 canary config 不需要 |

   舊格式的 gemini-cli `oauth_creds.json` 與 `~/.config/github-copilot/hosts.json` 對現行
   CLI 無效，adapter 會以檔名 allowlist 或形狀檢查拒絕。

## 4. 執行與確認

```sh
gh workflow run deployment-canary.yml --repo OWNER/paulsha-cortex --ref <candidate branch>
```

canary 以該 ref 的 HEAD 作為 candidate SHA。完成後分別確認：

- artifact `deployment-canary-<sha>` 內的 `qualification.json` 為 passed；
- probe repo 出現已 merge 的 PR（分支 `feature/<issue>-deployment-canary-probe`，body 帶
  `Closes #N`），issue 已關閉，OpenSpec change 已 archive，todo 全勾。

## 5. 每次 canary 後：重置

PR 已 merge、issue 已關、change 已 archive、todo 已勾滿，同一個 target 不能重跑。下一次
一律回到 §3 從**未修改的範本**建立新的 repo、新 issue，並更新三個 variables。舊 target
依保留政策 archive（`gh repo archive "$probe_repo"`）；本文件不刪除 repository。範本本身
只在 `qualification/probe-template/` 改版時才以 §2 重新推送。

## 6. 目前已知的阻斷（canary 仍無法走完）

以下是讀碼確認、尚未修正的缺口；在它們解決前，§3～§4 準備好的 probe 也無法讓 canary 通過：

1. **容器內沒有 probe repo 的本機 checkout**：Monitor 只掃本機 checkout，intake 需要
   snapshot 已有 `(repo, work_id)`；install config 的 `source_repositories` 只有
   `paulsha-cortex`，driver 裝完直接 intake，沒有 clone、登記或等待 snapshot。
2. **容器沒有 pytest**：gate 固定跑 `/usr/bin/python3 -m pytest -q`，reference image 與
   wheelhouse 都沒有 pytest。
3. **容器沒有 policy-check**：ship preflight 與 archive gate 會跑 `python3 -m policy_check`。
4. **Copilot code review**：Manager 帳號必須能對 private probe 請求 Copilot review，否則
   review gate 逾時轉 needs_human。
5. deck compile 的 workflow 路徑目前讀的是 `paulsha-cortex` 自己的 `.project-policy.yml`，
   probe 的 `preflight.steps` 只在 ship 的引擎 preflight 生效。
