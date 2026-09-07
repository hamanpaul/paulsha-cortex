---
status: accepted
work_item: execution-profile-contract
domain_breadth: 2
state_consistency: 2
invariant_count: 10
artifact_classes: [source, tests, documentation]
---

# Execution profile 契約 Todo

## Authority

Issue [#835](https://github.com/hamanpaul/paulsha-cortex/issues/835)，parent #829，
基準 `60a3ffa867377b0c86fa2f10e91fb9820d91938c`／PR #832。
[Spec](../../specs/execution-profile-contract-spec.md)、[Design](../../specs/execution-profile-contract-design.md)
為本計畫的完整 scope；accepted 不代表可直接派工。本件保留所有 A1–A6／I1–I10。
production 跨 profile/identity/resolution、launcher/adapter、planning runtime、registry/workflow，
故 domain_breadth=2；含 schema migration／重啟一致性，故 state_consistency=2。
依 feature-oneshot，舊算法 10/Red；#831 修正 spec_stability 後預計 8/Red，仍須人工拆分。

## Boundary

產品碼與測試只由 Cortex 開發，本 intake 不實作；下列未勾工作是母範圍交付清單，
不是將 Red 母件直接作單一 build 派工的指令。人工拆分須保留 parent AC owner、依賴、真實 sizing
及正常 authority/registration/freeze，不能刪驗收／降分；#833 自動分解尚未可用。
#842 是 qualification lifecycle owner；#581 保留五項解析／producer 原 scope，
PatchMUD #37 只提供 CLI/file schema／immutable fixture，不引入 runtime import。

## Tasks

- [ ] source T01：建立版本化 profile/descriptor/requirements 純契約與嚴格 parser，保留 requested/resolved/observed、原生 effort、unknown（A1、A4、A5；I1、I2、I8）。
      adapter/model/effort 擴充由資料表述，unknown role 不默認 build；role public seam 與 #581 對接。
- [ ] source T02：實作 canonical request/resolved/actual-condition key 與 versioned migration seam（A4、A5；I6、I7、I9）。
      精確條件變更要換 key，價格／時間戳不進能力 fingerprint；未知 observed 不外推 qualification。
- [ ] source T03：建立 adapter protocol/conformance seam，接 SubprocessLauncher、CLI factory 與 planning_runtime 的生產入口（A1、A2、A6；I2、I3、I10）。
      保留工具／sandbox、terminal、usage、quota capability、cancel/timeout 與 last-message；新 runtime 僅加受控 adapter。
- [ ] source T04：接 model identity/resolver、manager/daemon 與 autonomy 的完整 profile 傳遞與硬條件 preflight（A3、A6；I4、I5、I7）。
      pin 不降級且不豁免最低品質／independence／Trust Root；quota 未知不當零，也不在此加入 quota fallback。
- [ ] source T05：於既有 WorkflowRun／JobRegistry 寫入邊界增 versioned sibling binding，保留舊嚴格欄位與 frozen chain（A5、A6；I8、I9、I10）。
      隔離 state 驗證一致的舊／新版本；保留 attempt/evidence 原件，非 dispatch provenance 沿 #581 共用契約。
- [ ] source T06：接 #842 exact-profile qualification consumer 與 #581／PatchMUD #37 producer contract，沿用 map_report_to_envelope（A3、A4、A6；I5、I7、I10）。
      未核可／撤銷／過期／unknown 的回覆不得授予資格；發布與 CAS/crash 狀態機由 #842 負責，不在本票重做。
- [ ] tests T07：新增 descriptor-only 虛構 model/native-effort、canonicalization 與逐欄 perturbation 測試（A1、A4）。
      exact requested/resolved、不同產品 effort 不等價、unknown 不補值、價格／時間穩定均需 assert。
- [ ] tests T08：新增 fake adapter 與受控本地 subprocess conformance，argv/terminal/usage/quota capability/cancel/timeout/工具/sandbox（A2）。
      Event/barrier 取代碰運氣 sleep；unsupported 與每個 hard-gate 負例斷言 spawn_count=0。
- [ ] tests T09：新增 unknown role、錯 pin、qualification 不足、同 domain reviewer、Trust Root 不合法且 quota 足夠仍拒絕的負例（A3）。
      同時核對真正 resolver／preflight／factory，不只測 fake validator；真人批准不能由測試 receipt 替代。
- [ ] tests T10：新增 legacy/frozen-chain round-trip、未知版／殘缺 descriptor、registry 寫入 failpoint／重啟與歷史不變性測試（A5）。
      使用隔離 state 及真 serializer/loader，保存前後 bytes 或內容 digest 作 assert；不手填 evidence hash。
      明確負例：缺可信原 schema／policy snapshot 的歷史 run 重讀仍為 legacy/unversioned/unknown，
      不猜填今天／舊版、不回寫原 chain；缺正式 authority 重新接受的新版切換請求須拒絕。
- [ ] tests T11：跑既有 model-resolution/per-work-chain/launcher、planning runtime、Trust Root、card sandbox、model-profile 回歸與 production producer/consumer 整合（A6）。
      先離線 fake process，再以 PatchMUD #37 真 immutable fixture/revision 驗 CLI/file；fake 不冒充 real launcher。
- [ ] documentation T12：同步 README／配置及 CLI 文件，說明 profile 版本、原生 effort、pin/preference、unknown、migration、#581／#842／PatchMUD #37 邊界。
      新增模型／adapter 的擴充操作與 legacy 讀取語意一致，不給固定模型／agent／effort 推薦名單。
- [ ] CLI T13：同步受影響 help／解析與離線 descriptor 驗證的正負例，保留既有選項相容性；僅 --help／離線驗證不啟動模型。
      不因增加 descriptor 而繞過 executable trust 或 sandbox；實際 provider probe 留獨立授權 gate。
- [ ] changelog T14：由 Cortex delivery 新增 changelog.d/execution-profile-contract.md 並同步 CHANGELOG.md [Unreleased]，滿足 R-09。
- [ ] tests T15：執行 focused/full suite、CI 與帶真實 PR context 的 policy-check，檢查 R-09/R-16/R-19/R-22，記錄候選 SHA 與實際結果。
      三件組與 authority 有修訂時走正式流程，不改已 frozen run 或現場 registry。
- [ ] documentation T16：分帳記錄 implemented/tests/merged/installed/live 證據；交付真 launcher／installed／live 時另取所需權限及 #842 真人 receipt。
      未執行不得勾通過；產品碼只由 Cortex 派工，active Manager restart、付費評測及 deployment 不由 intake 自動授權。

## Dependencies

先人工拆分／審查／正式進件，再由 Cortex 實作 schema 與 adapter；resolver 使用 #581 role/provenance
與 #842 qualification 的受控接口，durable binding 整合所有入口。#581 eval producer 與 PatchMUD #37
immutable fixture 是 A6 真整合前置；#842 的真人 receipt（政策要求時）是 live 前置。
#600／#836–#840 只消費本件契約，各自實作 availability／quota／forecast／reservation／admission／status。
root 本輪僅採 schema-core 優先的人工規劃順序；其餘 child 候選仍須誠實重評／再拆，
本裁決未建立 issue、註冊 work item 或授權產品／runtime／真人 qualification 操作。

## Verification Status

產品實作、產品 tests passed、PR merged、installed、live accepted 目前均未完成。
本 intake 的純 planning helper 檢查與舊／預計新 sizing，另記於
[審查報告](../../../../reports/review/refine-profile-intake-20260907.md)，不充當 builder qualification。
