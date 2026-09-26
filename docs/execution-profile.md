# Execution profile 操作與相容性

Execution profile 描述一次模型執行請求、Cortex 實際選擇的執行條件，以及可信來源觀察到的執行結果。這是既有 model identity／model chain 的加法契約；不取代 resolver、Trust Root、review gate 或 launcher sandbox。

## 三個 profile plane

| Plane | 表示內容 | 可作為已發生事實嗎 |
| --- | --- | --- |
| `requested` | 呼叫者明示的 profile 偏好與 pin；未明示欄位為 unknown | 否 |
| `resolved` | resolver 與專用 launcher 契約最終選出的 adapter、model、native effort、persona loadout、toolset、sandbox、permissions 與 toolchain | 代表派工決定，不代表 process 已成功啟動 |
| `observed` | 可信 producer 對 exact `resolved_key` 回報且完整驗證的執行條件 | 是；缺資料或來源無法驗證時維持 unknown |

Profile schema 使用 `execution_profile.py` 的既有 descriptor/profile parser 與 canonical keys。`ExecutionProfileBinding` 以 schema version 1 保存三個 plane、`request_key`、`resolved_key` 及 `actual_key`。key 不納入時間戳或價格；`actual_key` 由 observed conditions 計算，未觀察時不等於 resolved key。

## Adapter 擴充

Adapter 是受信任的 Python 實作，透過 `ExecutionAdapter` 註冊。Descriptor 只能宣告 adapter/model/effort grammar 與 metadata，不能指定 Python import、命令或執行程式碼。既有 executor argv builder、terminal envelope、usage extractor、launcher process-group cancellation、job watchdog 及各自 sandbox 仍是執行來源；adapter 不建立較寬鬆的替代路徑。

新增 adapter 時必須提供穩定 adapter ID、protocol/runtime version、native effort grammar，並以 fake adapter 測試 argv、terminal、usage、quota capability、cancel/timeout、工具與 sandbox。未支援的 executor 或 effort 必須在 process spawn 前拒絕。沒有可信 quota producer 時，quota state 為 `unknown`；usage 計量不能推導剩餘 quota。

## Production dispatch 與資格

Manager 在已選定 identity 並套用 persona launcher policy 後建立 profile，執行硬條件檢查，再透過既有 launcher 啟動。Generic autonomy dispatch 與 planning invocation 同樣綁定並驗證 profile。`SubprocessLauncher` 在建 job／執行 process 前再次核對 executor、model、effort 與 sandbox，阻止持久化的 profile 和實際啟動參數漂移。

未知 persona role、與 resolved identity 不符的 pin、同 independence domain reviewer、既有 Trust Root 不相容，或缺少要求的 exact-profile qualification 都會 fail-closed。已 sizing 的 workflow 若尚無與 resolved key、role、完整 coverage 綁定且未撤銷的 #842 qualification，Manager 會記錄 `needs_human` 並停止在 spawn 前；profile/quota 不會繞過人工 review 或既有 authorization/CAS。

## 持久化與升級

`WorkflowRun.execution_profile_bindings` 是 optional、versioned sibling 欄位。缺欄位代表舊版 run，讀取時保留舊 `resolved_model_chain`、attempt 與 evidence，不回填推測值或改寫歷史。新版可讀取沒有 sibling 的舊 run；舊讀取器忽略此新增 top-level 欄位。若欄位存在，格式／schema version、descriptor、profile planes、keys、persona role 與 resolved identity 必須一致；未知版本或殘缺 binding 拒收。

寫入只新增 binding，不把 profile 資料塞入 frozen chain 或歷史 receipt。Profile 更新由本次新的 dispatch decision 建立；歷史 run 不做 migration。Registry 仍使用既有 manager update 與持久化流程。

## PatchMUD producer/consumer 契約

Cortex 的 `profile_report_consumer` 是純 consumer：呼叫端必須提供預期 resolved profile key、預期 source revision 與 canonical JSON payload digest。schema version、revision、digest 或 profile key 任一不符即拒收。Cortex 不匯入 PatchMUD runtime，也不自行發布 qualification、核可 report 或改寫外部 producer。

目前自動化測試使用固定的本地 fixture；整合真實 PatchMUD #37 immutable report/fixture 時，需核對 producer revision 與其 exact serialized digest，再執行跨 repo consumer 驗收。這項測試不等同 live model、installed launcher 或真人 qualification 驗收。
