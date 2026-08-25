---
status: accepted
work_item: phase2-install-docker-qualification
---

# Tasks

## 1. Baseline and prerequisites

- [x] 記錄 origin/main exact SHA、dirty/untracked preservation 與獨立 worktree boundary。
- [x] 逐 commit 重驗並最小納入 `c35516e`、`98978b6`，不整包採用 live-closeout branches。
- [x] 以 code/test/live issue evidence 重新裁決 #623/#665/#681/#692/#695/#716/#763；
  #665/#692/#763 的 source fixes 已整合，仍需 protected exact-SHA RC 證實部署面。

## 2. Trust-root install flow (TDD)

- [x] [RED] plan schema/canonical hash、secret/path rejection、four-way config、bundle/wheel identity tests。
- [x] [GREEN] `cortex install trust-root plan` 與 canonical desired-state model。
- [x] [RED] preflight、account collision、symlink escape、ACL order、partial replay、idempotency tests。
- [x] [GREEN] root-gated apply transaction、receipt journal、atomic venv slots、safe adoption。
- [x] [RED] credential whitelist/redaction/atomic import 與 activation guard tests。
- [x] [GREEN] provider adapters、credential receipt metadata、egress→Manager→Monitor activation/compensation。
- [x] [RED] generated-installed functional/comment drift、verify evidence、safe rollback tests。
- [x] [GREEN] inventory attestation、verify JSON/evidence 與 receipt-bounded rollback。
- [x] 保留 `cortex install service`；bootstrap 僅提示 trust-root，不 sudo、不搬憑證。

## 3. Docker qualification (TDD)

- [x] [RED] image/harness contract tests：Ubuntu 24.04、systemd PID 1、cgroup/tmpfs、禁止敏感 mounts、artifact-only install。
- [x] [GREEN] reference Dockerfile、entrypoint/local harness、fresh/idempotent/drift/rollback/reinstall stages。
- [x] [GREEN] selfcheck、registry equation、attestation、identity/hardening、R9 五族 attack/negative-control stages。
- [x] [GREEN] provider/model preflight、單次 Codex smoke、agy Gemini 3.7 Flash high、Copilot GPT-5.4 xhigh 與 terminal E2E hooks。
- [x] [GREEN] Manager GitHub credential-helper auth/dry-run push probe（不得改 remote ref）。
- [x] [GREEN] canonical `qualification.json`、redacted logs、artifact/service/provider hashes。

## 4. CI and release gates

- [x] 新增只允許 `workflow_dispatch` 的 RC qualification workflow，RC secrets 僅掛 protected environment。
- [x] 修改 release workflow：建立 Release 前下載、驗證同 commit qualification evidence 與 wheel hash；缺失/過期/不一致失敗。
- [x] 新增/修改 workflows 的 `uses:` 全部 40-hex pin；保留 Python 3.10–3.13、full pytest、build、twine、clean-wheel smoke。

## 5. Verification and closeout

- [ ] BLOCKER：以 latest candidate 重跑 protected exact-SHA qualification，並保留 R9 T2
  的六種 mutation denial 與合法 `repo-worktree` producer write evidence；本地 dummy
  credential run 已通過 R9，尚不能替代 protected provider/live evidence。
- [ ] focused tests、full pytest、build/twine/clean-wheel smoke 與 repo preflight 全綠。
- [x] #665 以 strict-compatible `/usr/bin/node --jitless` service wrappers 收斂
  `srt`／`openspec` W+X 缺口；#692 HOME 與 #763 Manager Git/recovery regression 已通過
  focused suite。
- [x] 每條 review finding 獨立驗證並分類修/駁/接受列管；修後重審。
- [x] 對抗審查只列 BLOCKER/MAJOR；未處置缺陷為 FAIL，明文有界殘餘風險本身不構成 FAIL（D6 不可 waiver）。
- [ ] OpenSpec validate/apply-complete 後 archive；Conventional Commit；依授權建立 PR。
- [ ] exact-SHA RC qualification 全綠後才允許 `v0.2.0` GitHub Release；否則明確停在 blocked，且不發 PyPI。
