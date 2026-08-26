---
status: executable
work_item: trust-root-phase2-closeout
phase: 2
audience: operator
supersedes: docs/superpowers/runbooks/trust-root-phase2b-setup.md
authority: transactional-installer
refs:
  - docs/superpowers/specs/trust-root-isolation-spec.md
  - paulsha_cortex/trust_root/install/cli.py
  - qualification/run.sh
---

# Trust Root Phase 2 transactional install

這是目前唯一可執行的 production 安裝／升級 runbook。舊的 Phase 2b 文件只保留歷史
診斷與決策脈絡，不得再照其中的 `rm`／`cp`／`chown`／`mv` 手工重播部署狀態。

## 邊界

- GitHub release 只發佈 immutable wheel；**不會**自動改動主機的 `/opt/cortex`。
- `plan` 不需 root，也不更動系統；只有 operator 明確執行的 `apply`、credential
  `import`、`activate`、`verify`、`rollback` 會進入 root 邊界。
- installer 不從 `$HOME` 猜測、搜尋或複製任何憑證。每一份 credential source 都由
  operator 明確選定，內容不放在 argv、receipt 或 log。
- receipt 是 apply／activate／verify／rollback 的 authority。遇到未知 drift 時會
  fail closed；不要以手工覆寫繞過。

## 1. 產生並三方確認 plan

先準備由同一個 release 產生的 `install-config.yaml` 與 `bundle.json`，再在非 root
shell 執行。以下三個值各有不同來源：CLI 回報值、plan 檔實際 digest、operator 人工
確認值；任一不相等就停止。

```bash
set -eu
umask 077

cortex_install_config=/absolute/path/to/install-config.yaml
cortex_bundle=/absolute/path/to/bundle.json
cortex_plan_dir=$(mktemp -d "${TMPDIR:-/tmp}/cortex-install.XXXXXX")
cortex_plan_path="$cortex_plan_dir/install-plan.json"
cortex_plan_result="$cortex_plan_dir/plan-result.json"
cortex_install_evidence="$cortex_plan_dir/install-verification.json"

test -f "$cortex_install_config"
test -f "$cortex_bundle"
cortex install trust-root plan \
  --config "$cortex_install_config" \
  --bundle "$cortex_bundle" \
  --output "$cortex_plan_path" >"$cortex_plan_result"

cortex_reported_plan_sha=$(python3 - "$cortex_plan_result" <<'PY'
import json
import sys
print(json.load(open(sys.argv[1], encoding="utf-8"))["plan_sha256"])
PY
)
cortex_observed_plan_sha=$(sha256sum "$cortex_plan_path" | awk '{print $1}')
test "$cortex_reported_plan_sha" = "$cortex_observed_plan_sha"

cortex_receipt_path=$(python3 - "$cortex_plan_path" <<'PY'
import json
import sys
print(json.load(open(sys.argv[1], encoding="utf-8"))["receipt_path"])
PY
)
test "${cortex_receipt_path#/}" != "$cortex_receipt_path"

python3 -m json.tool "$cortex_plan_path"
read -r -p "Type the reviewed plan SHA-256: " cortex_confirmed_plan_sha
test "$cortex_confirmed_plan_sha" = "$cortex_reported_plan_sha"
```

人工 review 至少確認 candidate SHA／wheel hash、四個 service accounts、所有目標路徑、
systemd units、polkit 規則、toolchain artifacts、required credentials 與 canonical
receipt path 都符合本次變更。不要只把畫面上的 SHA 複製回 prompt；確認值代表 operator
已閱讀 plan 並接受其完整 mutation set。

## 2. Apply exact plan

只有三方 SHA 一致後才進 root 邊界。`apply` 可重播同一 receipt；它不把 plan 翻譯成
手工 shell 操作。

```bash
sudo cortex install trust-root apply \
  --plan "$cortex_plan_path" \
  --confirm-sha256 "$cortex_confirmed_plan_sha" \
  --receipt "$cortex_receipt_path"
```

## 3. 明確匯入所需 credentials

只匯入 plan 的 `required_credentials` 列出的項目。下列四個 source path 必須由 operator
逐一指定到正確檔案；不要從任何 HOME 自動探索，也不要把 secret 值放進環境變數或命令列。

```bash
cortex_builder_codex_source=/absolute/operator-selected/path/auth.json
cortex_reviewer_agy_source=/absolute/operator-selected/path/oauth_creds.json
cortex_reviewer_copilot_source=/absolute/operator-selected/path/hosts.json
cortex_manager_github_source=/absolute/operator-selected/path/hosts.yml

sudo cortex install trust-root credentials import --receipt "$cortex_receipt_path" \
  --principal builder --provider codex --source "$cortex_builder_codex_source"
sudo cortex install trust-root credentials import --receipt "$cortex_receipt_path" \
  --principal reviewer-planner --provider agy --source "$cortex_reviewer_agy_source"
sudo cortex install trust-root credentials import --receipt "$cortex_receipt_path" \
  --principal reviewer-planner --provider copilot --source "$cortex_reviewer_copilot_source"
sudo cortex install trust-root credentials import --receipt "$cortex_receipt_path" \
  --principal manager --provider github --source "$cortex_manager_github_source"
```

來源檔的保留或銷毀由其原本的 credential 管理流程決定；installer 只管理 receipt 記錄的
目的地，不宣稱擁有 operator 的來源檔。

## 4. Activate、verify、必要時 rollback

```bash
sudo cortex install trust-root activate --receipt "$cortex_receipt_path"
sudo cortex install trust-root verify \
  --receipt "$cortex_receipt_path" \
  --json \
  --evidence "$cortex_install_evidence"

sudo systemctl is-active cortex-egress-proxy.service
sudo systemctl is-active cortex-manager.service
sudo systemctl is-active cortex-monitor.service
```

`verify` 必須回傳 PASS，且三個 units 都必須是 `active`，才可宣稱這台主機已部署。package
release、RC container success 或靜態測試都不能代替這個 live 結論。

若 apply／activate／verify 中止，使用同一份 root-owned receipt 回滾；rollback 只處理
receipt-owned state，偵測到未知 drift 時會保留並回報，需先人工裁決。

```bash
sudo cortex install trust-root rollback --receipt "$cortex_receipt_path"
```

## 5. Deployment canary

production host verify 與 protected GitHub deployment canary 是兩個 gate。canary 另外需要
四份 GitHub environment secrets 與三個非秘密 variables；workflow 會在 disposable
container 內安裝 exact wheel、跑完整 intake-to-closeout，並要求 `worktree-isolation`
確實由指定 Codex model 自主產生至少一筆成功且有輸出的 command event。沒有成功的 live
canary run 時，#716 必須維持 open。
