---
status: accepted
work_item: phase2-install-docker-qualification
---

# Tasks

## 1. Baseline and prerequisites

- [ ] 記錄 origin/main exact SHA、dirty/untracked preservation 與獨立 worktree boundary。
- [ ] 逐 commit 重驗並最小納入 `c35516e`、`98978b6`，不整包採用 live-closeout branches。
- [ ] 以 code/test/live issue evidence 重新裁決 #623/#665/#681/#692/#695/#716/#763；未證實者保持 release blocker。

## 2. Trust-root install flow (TDD)

- [ ] [RED] plan schema/canonical hash、secret/path rejection、four-way config、bundle/wheel identity tests。
- [ ] [GREEN] `cortex install trust-root plan` 與 canonical desired-state model。
- [ ] [RED] preflight、account collision、symlink escape、ACL order、partial replay、idempotency tests。
- [ ] [GREEN] root-gated apply transaction、receipt journal、atomic venv slots、safe adoption。
- [ ] [RED] credential whitelist/redaction/atomic import 與 activation guard tests。
- [ ] [GREEN] provider adapters、credential receipt metadata、egress→Manager→Monitor activation/compensation。
- [ ] [RED] generated-installed functional/comment drift、verify evidence、safe rollback tests。
- [ ] [GREEN] inventory attestation、verify JSON/evidence 與 receipt-bounded rollback。
- [ ] 保留 `cortex install service`；bootstrap 僅提示 trust-root，不 sudo、不搬憑證。

## 3. Docker qualification (TDD)

- [ ] [RED] image/harness contract tests：Ubuntu 24.04、systemd PID 1、cgroup/tmpfs、禁止敏感 mounts、artifact-only install。
- [ ] [GREEN] reference Dockerfile、entrypoint/local harness、fresh/idempotent/drift/rollback/reinstall stages。
- [ ] [GREEN] selfcheck、registry equation、attestation、identity/hardening、R9 五族 attack/negative-control stages。
- [ ] [GREEN] provider/model preflight、單次 Codex smoke、agy Gemini 3.7 Flash high、Copilot GPT-5.4 xhigh 與 terminal E2E hooks。
- [ ] [GREEN] Manager GitHub credential-helper auth/dry-run push probe（不得改 remote ref）。
- [ ] [GREEN] canonical `qualification.json`、redacted logs、artifact/service/provider hashes。

## 4. CI and release gates

- [ ] 新增只允許 `workflow_dispatch` 的 RC qualification workflow，RC secrets 僅掛 protected environment。
- [ ] 修改 release workflow：建立 Release 前下載、驗證同 commit qualification evidence 與 wheel hash；缺失/過期/不一致失敗。
- [ ] 新增/修改 workflows 的 `uses:` 全部 40-hex pin；保留 Python 3.10–3.13、full pytest、build、twine、clean-wheel smoke。

## 5. Verification and closeout

- [ ] focused tests、full pytest、build/twine/clean-wheel smoke 與 repo preflight 全綠。
- [ ] 每條 review finding 獨立驗證並分類修/駁/接受列管；修後重審。
- [ ] 對抗審查只列 BLOCKER/MAJOR；未處置缺陷為 FAIL，明文有界殘餘風險本身不構成 FAIL（D6 不可 waiver）。
- [ ] OpenSpec validate/apply-complete 後 archive；Conventional Commit；依授權建立 PR。
- [ ] exact-SHA RC qualification 全綠後才允許 `v0.2.0` GitHub Release；否則明確停在 blocked，且不發 PyPI。

