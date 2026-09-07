---
status: accepted
work_item: execution-profile-schema-core
domain_breadth: 0
state_consistency: 1
invariant_count: 10
artifact_classes:
  - source
  - tests
  - documentation
---

# Execution profile schema／key core Todo

## Authority

母 [#835](https://github.com/hamanpaul/paulsha-cortex/issues/835) T01/T02，依
[Spec](../../specs/execution-profile-schema-core-spec.md)／[Design](../../specs/execution-profile-schema-core-design.md)。
本 child owner 為 [#849](https://github.com/hamanpaul/paulsha-cortex/issues/849)，本批登錄 accepted 規劃；
尚未 freeze／dispatch，不因登錄而授權模型派工。
production 只有新 `coordinator/execution_profile.py` 單一資料流，domain=0；定義版本化 schema，
依 #208 rubric state=1。無既有 state migration／transaction，不能冒稱母件 migration 已完成。
feature-oneshot 的現行 79 runtime 純函式實算 7/Red；#831 完整穩定規劃=0 的情境為 5/Yellow，
後者僅投影，須等真修正版 runtime 重算與正式 gate，不能按投影直接當 Yellow 派工。

## Tasks

- [ ] source T01：只新增 execution_profile.py 純 descriptor/profile/requirements schema API；嚴格版本、原生 effort grammar、三層 plane/tagged unknown（C01/C02/C06/C07）。
      使用 stdlib，normal direct import 不改現有 __init__/loader/launcher/registry/CLI；不讀檔／環境或呼叫模型。
- [ ] source T02：實作 typed canonical bytes、分 domain record/actual key、unknown 無 actual key、metadata 排除與 immutable serializer（C03–C08）。
      逐 byte 遵守 Design D3/D4 完整欄位、projection、node tags/arity、JSON token／escaping、NUL／uint64 framing；無 approved／qualified API，role 是資料不是 build fallback。
- [ ] tests T03：新增虛構 descriptor 的 string/new value/integer/finite number/nested effort 正負矩陣與 exact requested/resolved/observed round-trip（C01/C02/C07）。
      unknown model/role 的 runtime eligibility 留母 resolver；不把 schema 合法視為相容性已過。
- [ ] tests T04：新增 canonical golden vectors、field perturbation、metadata-only、set/order/type/domain 差異及缺 observed 無 actual key（C03–C06）。
      直接使用 Design D4 兩個完整 canonical payload／key 與五個 int/float/string/signed-zero key；替換 node shape／framing／projection 的負控制須拒絕，不能用被測函式自產 expected；測量 pass 不產資格 grant。
- [ ] tests T05：新增 mutation alias、duplicate keys、bool version/number、NaN/Inf、surrogate、未知欄位、depth/nodes/bytes 邊界、循環／未來版負例（C07/C08/C10）。
      依 D5 精確計完整 descriptor+profile/root/keys，16/17、4096/4097、65536/65537、1048576/1048577 成對；transport/semantic 錯誤分開且提早中止，預解析／native／等價 JSON 路徑一致；D1 reference 正負 grammar；no-version legacy input 拒收但原 bytes 不變。
- [ ] tests T06：以公開 API 做 fixture consumer 與獨立 process／hash seed round-trip、純函式 I/O 邊界與負控制（C09/C10）。
      從 checkout 外讀已建 package 的新 module 驗 packaging/import；不因此宣稱 production routing／installed Manager 已接線。
- [ ] tests T07：跑既有 model identity/envelope/resolution/per-work-chain/profile CLI 回歸、focused/full suite 與 CI；保存實際 RED→GREEN／候選 SHA 與未執行項目。
- [ ] documentation T08：同步 README／純 API 契約、版本／數值規則、未知值、native effort 擴充與 parent AC 對照；#581/#842/PatchMUD #37 owner 邊界不得遺失。
      #842 只依 core 契約；母 T03/T04/T05/T06 的 routing/qualification/migration 接線仍另行交付。
      區分 core wire 升版與 adapter protocol 欄位新值；後者 descriptor-only／key 自然變。記載 D5 transport 界內的 encoding 等價保證及超大 padding 的獨立拒絕。
- [ ] CLI T09：既有 --help smoke 與文件說明本件沒有新 flags/commands，也不提供任何 model/agent/effort 固定清單或 runtime probe。
- [ ] changelog T10：正式 Cortex delivery 新增 changelog.d/execution-profile-schema-core.md 並同步 CHANGELOG.md [Unreleased]，本 planning-only authoring 不代寫。
- [ ] tests T11：以實際 PR context 跑 policy R-09/R-16/R-19/R-22 等適用 gate；只允許本 core production 檔，若必須改其他 consumer 則停止並重裁 scope/sizing。
- [ ] documentation T12：分帳 core implemented/tests/merged/package import 與母 routing/installed/live；未知或 legacy qualification 不補值，真人批准仍由 #842 政策要求的 receipt 決定。

## Dependencies

本件 pure core 可獨立建測；其 schema/key → #842 publication consumer，沒有母件 close 的反向依賴。
母件保持 A1–A6 完整責任；legacy state migration／authority 重新接受與 actual routing 不在本件。
未修 #831 前現行 Red 必須照實保留；不得降低 schema 的 state 宣告或刪 AC 取得 Yellow。
本批只交付規劃與 owner 登錄；產品開發須另走正式 Cortex workflow，不修改現場 state 或服務。

## Verification Status

本輪只跑 planning 純 helper；結果與 source revision、hash 交接見
[報告](../../../../reports/review/refine-profile-schema-child-20260907.md)。
產品 tests／實作／merge／installed／live 尚未完成，所有產品 checkbox 保持未勾。
