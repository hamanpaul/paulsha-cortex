---
status: accepted
work_item: release-qualification-boundary
---

# Tasks

- [x] [RED] 新增 workflow regression：`rc-qualification` 不得使用 environment/secrets/probe variables，
  必須以 `release` profile 執行；`deployment-canary` 才能持有 protected live inputs。
- [x] [RED] 新增 schema/validator regression：release evidence 允許空 providers、拒絕 live artifacts/tests；
  canary evidence 必須具備 provider/GitHub/full-dispatch semantics，兩者不可互換。
- [x] [RED] 新增 runner/redaction regression：release profile 不讀任何 `CORTEX_RC_*` secret，canary
  profile 仍對缺少 secret fail closed。
- [x] [GREEN] 將 qualification evidence 升為 schema v2，加入 `release`／`deployment-canary` profile。
- [x] [GREEN] 分流 runner/driver：共同執行 installer/systemd/attestation/attack gates；只在 canary 執行
  provider smoke、Manager GitHub probe 與 full dispatch。
- [x] [GREEN] 將 rc workflow 改為 deterministic release profile，並新增受保護的 manual canary workflow。
- [x] [GREEN] release workflow 改用 `--require-release-profile`，維持 exact-SHA/freshness/hash join。
- [x] [GREEN] 更新 redaction scanner、CHANGELOG、fragment 與 release qualification 操作文件。
- [x] 驗證 focused/full pytest、schema、active OpenSpec strict validation、build/twine 與 clean-wheel smoke。
- [x] 逐項自我對抗 review：release profile 不會呼叫 live functions、runtime 無網路、profile 不可互換、
  candidate build recipe 相同，且 canary artifact 不被 release workflow 引用；查無未處置 BLOCKER/MAJOR。

## External closeout gates

以下步驟刻意不偽裝成 archive 前可完成的 implementation checkbox：

- 封存本 change、驗 canonical specs，並以真實 PR metadata 跑 policy/preflight。
- conventional commit、push、PR、final-head checks 與 `paulc-arc` 獨立 approval 後才 merge。
- merge 後在 exact main SHA 跑 deterministic qualification，再執行並驗證 `v0.1.9` GitHub Release。
