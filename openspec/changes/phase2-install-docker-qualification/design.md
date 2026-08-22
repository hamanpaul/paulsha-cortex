---
status: accepted
work_item: phase2-install-docker-qualification
---

## Context

Phase 2 的權限真相目前由 `paulsha_cortex.trust_root.registry` 與 `permgen` 提供，但安裝仍
仰賴 operator 手動把產生內容落到 `/etc`、`/opt/cortex` 與 `/var/lib/cortex*`。這使
generated state、installed state 與實際執行 candidate 之間沒有單一 transaction identity。
此外 release CI 僅在一般 runner 的 venv 中驗 wheel，無法承載 systemd PID 1、polkit、
ACL、cgroup v2 與多使用者的 OS-level semantics。

## Decisions

### 1. Plan 是唯一 desired-state 輸入

`cortex install trust-root plan` 在非 root 身分執行，讀取明示 config、candidate bundle
與 repo identity，輸出 canonical JSON。plan 固定 `four-way`，包含 candidate/bundle hash、
principal、asset、owner/mode/ACL、unit、polkit、shim、gitconfig、toolchain manifest、legacy
quarantine policy 與 apply order；schema 明確拒絕 secret-like 欄位與不安全路徑。

`apply` 只接受 plan 與 `--confirm-sha256`，驗 canonical digest 後直接消費 registry/permgen
結構化資料；不得執行 plan 夾帶的 shell 字串。

### 2. Receipt 是 transaction 與 rollback authority

apply 在 mutation 前完成 systemd、polkit、cgroup v2、ACL、sudo policy、disk、in-flight
jobs、service state、account collision、symlink/path escape preflight。每一步先記錄原 metadata、
ACL、被替換 bytes 的 content-addressed backup，再原子落檔並 append step journal。

receipt 固定 root-owned、不可含 credential bytes。中斷後重跑同 plan 只能 replay 未完成步驟；
已存在資產只有 identity、owner/mode 與 receipt 完全吻合才 adopt。rollback 只反轉 receipt
明列且仍匹配 installed hash 的資產，絕不遞迴刪除未知或既有 durable state。

### 3. Candidate runtime 採雙槽原子切換

apply 從 hash-locked wheelhouse 安裝 exact candidate 到 `/opt/cortex/venv.new`，完成 import、
version、artifact hash 與 generated inventory 驗證後才原子切換 `/opt/cortex/venv`；前一槽保留
供 rollback。部署期只建立 workspace container，不預先廣開 Builder/Gate 的 per-job ACL。

### 4. Credential import 與 activation 分離

provider adapter 以 `(principal, provider)` 白名單決定可接受的 regular files、owner/mode 與
目的地。source symlink/special file 一律拒絕；寫入使用 temp+fsync+rename。stdout、log、receipt
與 JSON 只記 provider、principal、mode、sha256，不記內容或 source HOME 掃描結果。

`activate` 先要求 receipt 已完成 apply 與所需 credential imports，再依 egress proxy → Manager
→ Monitor 啟動；任一步失敗即反向停止。只有 `verify` 產生成功 evidence 後，receipt 才進入
activated/qualified 狀態。

### 5. Attestation 以完整 inventory 比對

inventory 從 registry/permgen 機械導出 units、shim、polkit、gitconfig 與 toolchain wrappers。
expected 與 installed 逐項 hash，另計算 normalized functional lines：功能差異為 FAIL，只有註解
差異為 WARN。缺項、額外 authority-bearing 項或 generator error 皆 FAIL。

### 6. Docker qualification 使用 artifact-only runtime

Ubuntu 24.04 image 以 systemd 為 PID 1，安裝 polkit、acl、sudo、git、bubblewrap、socat、Node
與必要工具。harness 只掛 cgroup v2、`/run`/`/run/lock` tmpfs、candidate artifacts 與獨立 data
volume；不掛 checkout、host HOME、Docker socket或未列管 credential directory。容器內只從
wheelhouse 安裝，禁止 editable install 或 `PYTHONPATH` 偷讀 checkout。

正式 workflow 僅 `workflow_dispatch`，使用受保護 RC environment。它輸出 schema-versioned
`qualification.json` 與 redacted logs，綁 candidate SHA、wheel/bundle/image digest、每個 test、
provider/model metadata、service identity 與 artifact hashes。quota/login/model mismatch/fallback/
SKIP 皆為失敗且不自動 retry。

### 7. Release exact-SHA join gate

release workflow 在建立 tag 對應的 GitHub Release 前，下載由 RC workflow 產出的 evidence，驗：
成功 conclusion、candidate SHA 等於 tag commit、wheel hash 等於 release 當次 build、schema 與
freshness policy、所有 required test/pass 以及 artifact signature/digest。任何缺失或不一致 fail
closed。D6 不接受 residual-risk waiver。

## Safety and Failure Semantics

- 所有路徑先做 lexical 與 resolved containment 檢查，任何 symlink parent 或 escape 失敗。
- 所有 subprocess 使用 typed argv；不使用 `shell=True`。
- universal negative claims 由 runtime invariant/attack tests 守護，靜態 inventory 僅標為當下佐證。
- receipt/evidence 寫入採 canonical JSON、fsync file、atomic replace、fsync directory。
- error renderer 只輸出 redacted structured fields，不輸出 env、credential bytes 或 exception repr。

## Rollout

1. 合入 prerequisite fixes 與 installer/qualification code，但不改 production deployment。
2. 由手動 RC workflow 對 exact candidate 執行 qualification。
3. 只有 OpenSpec archive、雙向 review、preflight、對抗審查、全部 CI 與 RC qualification 均綠，
   才建立 `v0.2.0` GitHub Release；PyPI 保持禁用。
