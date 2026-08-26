---
status: accepted
work_item: trust-root-copilot-toolchain-pinning
---

# Trust-root Copilot toolchain pinning closeout

Issue: `hamanpaul/paulsha-cortex#681`.

## Outcome

舊 PR #789 的手動 Copilot package-tree publisher 不再是權威實作，也不得回灌：它的
temporary directory 未保證與目的地同 filesystem，且對既有 non-empty install tree 的
rename 不具 idempotency。Phase 2 已以較強的現行路徑取代：

- `.github/workflows/rc-qualification.yml` 取得明確版本與完整 SHA-256 綁定的 native
  Copilot artifact，不從 operator HOME 或 ambient PATH 發現 payload。
- `paulsha_cortex.trust_root.install` 產生直接 exec `/opt/cortex/toolchain/lib/copilot`
  的 root-owned wrapper，驗證 path/hash/owner/group/mode，並以 lock、staging、receipt
  rollback 執行 transactional install。
- coordinator 的 job PATH 固定把 `/opt/cortex/toolchain/bin` 放在最前；exact-main
  `v0.1.9` RC evidence 已驗 Copilot 1.0.80 與 installed wrapper hash。

## Acceptance

- [x] pinned payload 與 wrapper 不依賴 operator HOME／ambient PATH。
- [x] payload、wrapper、metadata 與 installed inventory 由同一 install plan 綁定。
- [x] apply／reapply／rollback 走 transactional installer，不採用舊 publisher。
- [x] deterministic RC 以 exact candidate SHA 驗證實際安裝後 identity。
- [x] work-item authority 已移除不存在的 OpenSpec 與舊 PR #789 實作指向。

`permgen.build_toolchain_plan()` 保留為舊版 reference-only 文字產生器；它不是 Phase 2
transactional install authority。若未來要移除該相容 CLI，應另立 deprecation work item，
不得用它取代本頁所列的 installer／RC 證據。
