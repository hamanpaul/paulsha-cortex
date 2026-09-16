# 有界 child 整合核對

此報告只記規劃交付；產品仍未實作／merge／installed／live。兩個 designs 的 bytes
保持原獨立審查版本，root 只整合 owner、登錄、狀態與進度文字，未改核心契約。

## 審查與新讀者核對

- Schema R1 canonical wire 未定義的 MAJOR 已修，R2 PASS；fresh reader 的 wire
  版本、transport padding 上限、reference grammar 三項歧義修後逐項確認，fresh compact
  R3 對最後 schema/store 八份文件 PASS。Root 另以獨立 encoder 重驗兩個 golden：
  actual canonical/framed 920/963 bytes、key
  `cccfc64a59aed4d4f3c14caa2468b18ff1ea067eeb84e3f376e0e722799e9ac2`；
  observed 1227/1272 bytes、key
  `e2750040b4f91c563760daa19bcaa808f66bab7f4c445c3765522f19609b841b`。
- Store R1 無界 event ledger 的 MAJOR 已修為固定 bytes/depth/nodes/events/identity/
  sort/fold/work/deadline 界，R2 PASS；fresh reader 五題確認，process-death 歧義補
  K0/K1/K2 真正終止測試自建 process、fresh reader/flock/durability/replay oracle。
  SIGKILL 不等真斷電；不可中斷的外部 POSIX I/O 殘餘已明示。
- 以上是規劃 review／pure oracle，不是產品測試或模型 qualification evidence。

## Root 整合後重驗

2026-09-07 13:29 UTC，從 checkout 外載入 runtime
`79ba644780bf1c697c722ac24a297e7d02416100` 的真 planning/deck/gate 純函式：

| Owner / work | 真 sizing / combo | 純 gate / 負控制 |
|---|---|---|
| #849 execution-profile-schema-core | 0+1+2+2+2=7 Red；feature-oneshot 11 cards/11 bindings/4 gates | 三份 accepted、complete；R09/R16/R19/R22 coverage ready；缺 Tasks 拒收 |
| #850 executor-backoff-store-core | 0+2+2+2+2=8 Red；fix-standard 9 cards/9 bindings/2 gates | 三份 accepted、complete；相同 coverage ready；缺 Tasks 拒收 |

兩者 envelope 仍 `bypass: envelope_unavailable`，不代表 builder 封套合格。
#831 修正後的 5/6 Yellow 僅推算；必須實際 loaded runtime 重評，不捏造現行 band。
每張 issue 已建立、全文讀回且 OPEN；work-items 各自只綁該唯一 owner。

## 邊界與後續

- Schema 只供純 typed descriptor/profile/key；key 不是資格，unknown observed 無
  actual key。#835 routing/migration 與 #842 human-review publication 未完成。
- Store A 不驗真 terminal authority；C/D immutable inventory/provenance/reconciliation
  以及所有 admission lane 仍由 #825 後續 child 交付，raw valid 不等 quota-positive。
- PatchMUD #37 僅開票，不冒充 producer 實作／實測／真人 qualification。
- 本批有自己的 changelog fragment、Unreleased entry 與 PR-context preflight／CI
  gates。未取得 exact-head gate 結果前不得把本報告當作 PASS receipt。
