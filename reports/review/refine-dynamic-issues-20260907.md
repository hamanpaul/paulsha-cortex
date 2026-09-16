# 動態派工 child issues 對照（2026-09-07）

## 本次結果與邊界

查證時間：2026-09-07 09:00 UTC。依主 agent 核可的 D1–D10 規劃地圖，只對 D1/D4/D5/D6/D7/D8/D10
精確查重並開票；新增 7 張，沒有重複建立同 scope 工單。既有 #581/#600/#828 不修改，保留原 owner。
原 issue-authoring 子任務只做 GitHub issue 建立與本報告新增；當下未改產品碼、formal plan、registry、服務或 native runtime，
未執行 commit/push/PR/派工/模型評測/付費 probe/部署/restart；後續 root 納入正式規劃的提交另列於 plan ledger。

已唯讀確認 [PR #832](https://github.com/hamanpaul/paulsha-cortex/pull/832) 於
2026-09-07 08:46:58 UTC 合併，merge `60a3ffa867377b0c86fa2f10e91fb9820d91938c`。
該 revision 對先前 source 基底 `79ba644780bf1c697c722ac24a297e7d02416100` 的 compare
只有文件／治理資料變更，故各新票以 merge SHA 的 immutable source links 引用已核對入口。
這不表示服務已安裝或載入該 revision。

## Issue 對照與 read-back

每票建立後均用 `gh issue view --json number,title,body,url,state` 讀回，逐項比對送出內容。
下列「一致」只證明 issue 內容寫入正確，**不是實作或測試通過**。

| 地圖 ID | 建議 work_id（尚未註冊） | Issue | read-back | 最小依賴／原票邊界 |
|---|---|---|---|---|
| D1 | `execution-profile-contract` | [#835](https://github.com/hamanpaul/paulsha-cortex/issues/835) | number/title/body/url 精確一致；OPEN | profile/adapter/fingerprint 契約；沿用 #205/#534，與 #581 的 unknown-role 切換協調，不吸收 #483/#475 原修復。 |
| D4 | `quota-observation-provenance` | [#836](https://github.com/hamanpaul/paulsha-cortex/issues/836) | 精確一致；OPEN | #835；沿用 #325，#825 僅最小 durable backoff。本票 shadow 觀測，不做 reservation/routing。 |
| D5 | `operational-usage-forecast` | [#837](https://github.com/hamanpaul/paulsha-cortex/issues/837) | 精確一致；OPEN | #835/#836；重用 #325 與 EngineeringOutcome。#137/#138/#210 為 closed 設計，不重開，不重建 outcome ledger／泛用 RL。 |
| D6 | `shared-quota-reservation` | [#838](https://github.com/hamanpaul/paulsha-cortex/issues/838) | 精確一致；OPEN | #835/#836；可用固定需求先做。多 Manager live 另依 #818；#381 spawn 間隔不是共享 quota authority。 |
| D7 | `quota-aware-admission` | [#839](https://github.com/hamanpaul/paulsha-cortex/issues/839) | 精確一致；OPEN | #835/#836/#837/#838、#600，以及 #825/#826/#497/#830；qualification 發布鏈未決部分另列 gap，不能假設 #581 全包。 |
| D8 | `decision-status-projection` | [#840](https://github.com/hamanpaul/paulsha-cortex/issues/840) | 精確一致；OPEN | #839/#828；消費 #527 reason/next-actions 與 #827 freshness。只新增 decision receipt 跨 section 投影，不重做 needs_human 根因修復或 actual identity producer。 |
| D10 | `loaded-runtime-attestation` | [#841](https://github.com/hamanpaul/paulsha-cortex/issues/841) | 精確一致；OPEN | 沿用 #695/Trust Root receipt；#548 service-env 原票不重做；真部署前保留 #800/#815/#476/#818/#582 各票驗收。 |

所有新票均含：#829/PR #832/immutable plan、現有 source 與非重做範圍、明確依賴、deterministic／
negative／restart 驗收、真實 sizing 待評、implemented/tests/merge/installed/live 分帳、
PatchMUD #37 外部邊界、禁止本票建立被當作付費評測或現場 restart 授權。未加 assignee/label，
未註冊 work_id，未替新工作選固定 model/agent/effort。

## 查重方法與相鄰範圍

先以 GitHub open issue 搜尋逐項查 execution profile/descriptor、quota observation/remaining bounds、
usage forecast/track-record、shared quota/reservation、quota-aware/cost-aware/fallback、
decision receipt/provenance、loaded-runtime/artifact revision。另對全體 115 張 open issues 的
title/body 做候選篩選，再閱讀相鄰 issue；建立前逐一 exact 搜尋 7 個 work_id，均無命中。
也以標題補查額度預留、用量預估、載入 revision。這是當下查重證據，不是對未來新增 issue 的保證。

| 範圍 | 查得相鄰項目與處置 |
|---|---|
| D1 | #483 特定 effort repro、#475 自訂 executable、#581 解析/producer 殘項不等於共同 profile 契約；新 #835 明確排除其獨立修復。 |
| D4 | #825 明文不做共享帳號/token/cost 感知；#833 只是使用既有/後續 budget seam 的拆分 planner，本票不重做其工作。 |
| D5 | #137/#138/#210 closed 設計與已存在 #325/outcome 可重用；#506 GitHub polling、#781 I/O 放大不是 profile 任務預估。 |
| D6 | #818 registry single writer、#381 spawn 間隔與 #833 decomposition budget 是相鄰，不是跨 pool all-or-none 耐久 reservation。 |
| D7 | #825 最小退避、#830 合法非 Job consumer、#807 terminal、#826 taxonomy 各留原 scope；新 #839 是觀測/預估/預留後的共同准入與安全 fallback。 |
| D8 | #527 已有 needs_human reason/status/next-actions 原始缺陷；新 #840 只擁有 receipt 投影，將 #527/#827/#828 列消費／前置，不另吞根因修復。 |
| D10 | #548 effective service env、#815 installer rollback、#800 config guard、#695 installed asset attestation 均可區分；新 #841 只補 loaded process 與磁碟/配置 revision 的實證對照。 |

查重期間相鄰新增編號 #834 經查是 bootstrap 文件 PR，不是相同產品 scope 的 issue。

## 保留的缺口與 root 待裁決

1. **完整 qualification 發布鏈仍未有獨立 owner。** #581 原文五項涵蓋 doctor planning identity、
   unknown persona、launcher provenance、公開 overlay API、PatchMUD eval producer；它沒有明文
   承包 profile-aware qualification 發布／撤銷、human review receipt 的完整生命週期與 migration。
   #534 有人工複核與 evaluated-roster 方向，#835 提供 key/schema，但不能據此宣稱上述全鏈已具體
   進件。新 #835/#837/#839 已將此列 #829 gap；本次沒有擴 #581，也沒有擅開第 8 張新票。
2. **PatchMUD 外部 gate。** [PatchMUD #37](https://github.com/hamanpaul/paulsha-patchmud/issues/37)
   負責 profile-aware report/fingerprint/usage/schema，Cortex 可先用固定 fixtures；真
   report→qualification→review receipt→approved roster→實際派工需要 immutable fixture/revision。
   角色 deck #13、難度 #21、agent-native #12 保持不同依賴；未測/未核可不得外推。
   PatchMUD 不供 Cortex 帳號 remaining，不擁有 reservation/routing/loaded-runtime authority。
3. **B5 既有依賴仍獨立。** #818/#800/#815/#476/#548/#582 要按原票現況驗收或收口，
   #695 成果沿用；不能因 #840/#841 完成就關閉 B5。#828 與下游 paulshaclaw #328 保留獨立 owner。
4. **進件尚未發生。** 7 個新 scope 都需正式 spec/todo/authority 與 Cortex 真 sizing；地圖不保證
   每單 Yellow，若 Red 用 #833 受治理分解，不改低分數、不刪 AC、不手造 evidence。

## 檔案與驗證狀態

本次僅新增本報告；既有 `refine-dynamic-intake-map-20260907.md` 及先前 intake corrections 未修改。
branch：`feature/refine-intake-corrections-20260907`。只做文件 whitespace/scope 檢查，不跑產品測試。
issue read-back 共 7/7 精確一致；新票實作／測試／合併／安裝／live 均未完成。

## 後續 scope 裁決：qualification lifecycle 獨立 owner（#842）

**本節是原七票完成後，root 明確擴定的後續範圍，不是原七票擅自增項。** 上述「未有獨立
owner／未開第八票」描述原批次的查證時點；經此裁決，該 owner 缺口現在由下列新票承接，
但 qualification 產品功能、真批准與 live acceptance 仍未完成。

- 新 issue：[#842 — profile qualification 核可發布與撤銷生命週期](https://github.com/hamanpaul/paulsha-cortex/issues/842)。
- 建議 work_id：`profile-qualification-publication`；尚未註冊，sizing 待 Cortex 真評。
- 唯一 scope：versioned report→qualification candidate→明示 human-review receipt→approved
  roster 的發布／撤銷／有效期／legacy migration／CAS 與 crash 原子一致性。
- 查重：exact work_id、qualification publication／資格發布標題，以及當時 122 張 open
  issues 的 title/body；只有 #829/#835/#837/#839 記載未分配 gap，未找到相同 lifecycle owner。
- 重用 #581 producer 與解析五項、#534 核可政策、#835 profile key/fingerprint、既有
  `map_report_to_envelope`、approved 必附 reviewer/reviewed_at 與原子檔案替換。
  不擴 #581，不重做 benchmark/評分；#839 消費有效資格，#840 只投影。
- PatchMUD #37 供 producer schema／immutable fixture；零 runtime import；價格 provenance
  不進能力 fingerprint。舊 report 的樣本數不等於可證明的完整 encounter coverage。
- 11 項 AC 涵蓋 effort/loadout/adapter/model/role/coverage mismatch、unknown/legacy、未核可、
  撤銷／到期、replay、CAS/process barrier、crash/restart、歷史 attempt/evidence 不改寫及指紋。
- 若實際 approval policy 要求真人，live gate 必須取得該真人對 exact qualification 的合法
  receipt；fixture、agent review、issue/plan 核可都不能冒充。這個 gate 不因開票而通過。
- 已建立並逐票 read-back：number/title/full body/url 精確一致、state=OPEN。只建立 #842
  及追加本節，未改其他 issues/code/registry/runtime；目前累計 8 張新票的內容 read-back
  均一致，所有產品交付狀態仍需各自實證。
