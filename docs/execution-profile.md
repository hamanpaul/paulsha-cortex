# Execution profile 操作與相容性

Execution profile 描述一次模型執行請求、Cortex 實際選擇的執行條件，以及可信來源觀察到的執行結果。這是既有 model identity／model chain 的加法契約；不取代 resolver、Trust Root、review gate 或 launcher sandbox。

## 三個 profile plane

| Plane | 表示內容 | 可作為已發生事實嗎 |
| --- | --- | --- |
| `requested` | 呼叫者明示的 profile 偏好與 pin；未明示欄位為 unknown | 否 |
| `resolved` | resolver 與專用 launcher 契約最終選出的 adapter、model、native effort、persona loadout、toolset、sandbox、permissions 與 toolchain | 代表派工決定，不代表 process 已成功啟動 |
| `observed` | 可信 producer 對 exact `resolved_key` 回報且完整驗證的執行條件 | 是；缺資料或來源無法驗證時維持 unknown |

Profile schema 使用 `execution_profile.py` 的既有 descriptor/profile parser 與 canonical keys。`ExecutionProfileBinding` 以 schema version 1 保存三個 plane、`request_key`、`resolved_key` 及 `actual_key`。key 不納入時間戳或價格；`actual_key` 由 observed conditions 計算，未觀察時不等於 resolved key。

## Adapter descriptor（資料）與 adapter 實作（程式碼）

Production 的 adapter 能力分成兩半：

| 面向 | 來源 | 內容 |
| --- | --- | --- |
| descriptor（資料） | packaged `paulsha_cortex/coordinator/data/execution-adapters.yaml`，加上 config root 的 operator overlay `$PSC_PROJECT_CONFIG_ROOT/execution-adapters.yaml` | protocol／runtime version、原生 effort 的合法值、adapter 預設 effort、model 專屬預設 effort、usage／quota 標籤 |
| 受信任實作（程式碼） | launcher 已登記的 argv builder（copilot／claude／codex／agy／cg），或以 `execution_adapters.register_adapter()` 登記的新 runtime | argv、effort 通道、usage extractor、共用 terminal envelope、launcher process-group cancellation、job watchdog、sandbox |

`execution_adapters.py` 不再保存寫死的 adapter 能力表。resolver（`resolve_profile`）、argv builder（`build_copilot_argv`／`build_codex_argv`／`build_cg_argv`）與 launch 前的 profile 再驗證都讀同一份 descriptor，所以 profile 上的 resolved effort 就是 argv 實際發出的值。中央 resolver 不含 executor 或型號分支；codex 的 model 專屬 effort 也寫在 descriptor 的 `model_defaults`，不寫在程式碼。

### Descriptor schema（v1）

```yaml
schema_version: 1
adapters:
  codex:                         # 必須已有受信任程式碼
    protocol_id: openai-codex-cli
    protocol_version: "1"        # 字串
    runtime_version: cortex-adapter-v1
    usage_source: codex-jsonl
    quota_state: unknown         # supported / unsupported / unknown
    effort:                      # 沒有原生 effort 的 adapter 寫 null
      values: [low, medium, high, xhigh, max]
      default: null              # 或 values 其中之一
      model_defaults:            # 選填；model → values 其中之一
        gpt-6-luna: max
```

- **位置與優先序**：packaged 檔是內建預設；overlay 以同名 adapter 條目**整筆取代**資料欄位（不做欄位層級合併），沒列到的 adapter 沿用 packaged。程式碼登記的 adapter 排在兩者之間：overlay 可以調整它的資料，但不能拿掉它的程式碼 hook。
- **驗證（fail-closed）**：`schema_version` 缺少或不是 `1`、未知鍵（包含 `argv`、命令、路徑之類）、缺必要欄位、版本欄位不是字串、effort 值重複、`default`／`model_defaults` 不在 `values` 內、`quota_state` 不合法，一律拒收。overlay 不合法時**不會**退回 packaged 預設。
- **只能描述已有受信任程式碼的 adapter**：descriptor 不能新增可執行的 runtime。宣告原生 effort 的 adapter，其 argv builder 必須有 `effort` 參數；claude 與 agy 目前沒有 effort 通道，替它們宣告 effort 會被拒收。
- **生效時機**：overlay 在每次查詢時比對檔案 stat，檔案變動後的下一次派工就套用新內容，不需重啟 Manager。這份 overlay 不在 #841 loaded runtime attestation 的範圍內；實際生效值會寫進每次派工持久化的 binding（descriptor 與 `resolved_key`）。
- **錯誤時的派工行為**：descriptor 不合法屬於部署設定錯誤，不歸屬任何一個 run。Manager 在建立任何候選、worktree 或 job 之前拒絕整次派工，交給 tick 隔離處理，並**不**把 run 寫成 needs_human；修正 descriptor 後，下一個 tick 會自動恢復。已持久化的 binding 自帶 descriptor，所以 registry 仍可讀取。

新增 model 或既有協定的原生 effort 時只改 descriptor，例如在 overlay 為 copilot 加一個 effort 值，並用 `model_defaults` 指定新 model 的預設；Python 程式碼不需要改。改動會改變新派工的 `resolved_key`，既有 run 的 binding 與 qualification receipt 不會被回寫。

### 新 runtime（新 adapter）

新 runtime 必須以程式碼登記：`register_adapter(ExecutionAdapter(..., argv_builder=..., usage_extractor=...))`。登記時會檢查：必須有受信任的 argv builder；terminal／cancel／timeout 契約必須是 launcher 已實作的 `shared-terminal-contract-v1`／`launcher-process-group-v1`／`job-watchdog-v1`；effort 規則同 descriptor。登記後，`SubprocessLauncher` 才接受這個 executor，並依 argv builder 簽名傳入 `commit_required`／`effort`。Conformance 測試（`tests/test_execution_profile_integration_835.py::test_a2_registered_adapter_conformance_through_production_launcher`）以虛構 runtime 逐項驗證 production 路徑的 argv／spawn、terminal、usage、quota capability、cancel／timeout、工具與 sandbox。未支援的 executor 或 effort 一律在 process spawn 前拒絕。沒有可信 quota producer 時 quota state 為 `unknown`，不能從 usage 計量推導剩餘 quota。

## Production dispatch 與資格

Manager 在已選定 identity 並套用 persona launcher policy 後建立 profile，執行硬條件檢查，再透過既有 launcher 啟動。Generic autonomy dispatch 與 planning invocation 同樣綁定並驗證 profile。`SubprocessLauncher` 在建 job／執行 process 前再次核對 executor、model、effort 與 sandbox，阻止持久化的 profile 和實際啟動參數漂移。

未知 persona role、與 resolved identity 不符的 pin、同 independence domain reviewer，以及既有 Trust Root 不相容都會 fail-closed。

- **Trust Root**：`validate_dispatch_requirements()` 的 `trust_root_valid` 已改為必填，呼叫端不傳就是 `TypeError`，不再有永遠成立的預設值。Manager 的 workflow 派工與 slice lane 以 `trust_root_compatibility()` 取得判定結果，判定依據是既有 `model_resolution` 相容性契約：launcher profile，加上 persona principal 的 Trust Root toolchain grant 與 credential grant。這個契約只在加固 runner（`PSC_JOB_RUNNER` 不是 `direct`）下生效；direct 模式沒有 Trust Root 契約。runner 設定無法解析時 fail-closed。slice lane 延續 #381 不看 capability，其餘三層照樣檢查。
- **持久化**：卡片沒有 runtime 需求宣告時，profile gate 拒絕後會把 `execution-profile-blocked` needs_human 寫進 run，不再讓例外打穿 tick。有需求宣告的卡走 runtime preflight gate，Trust Root 拒絕在那裡是候選層級的拒絕：改派下一個合規候選；全部候選都被擋時，寫入 `runtime-preflight-capability_missing` needs_human，理由指名 `Trust Root profile is not valid`。
- **未知 role**：legacy manifest 裡的未知 persona（#581）在候選選擇之前就以 `execution-profile-blocked` 持久化 needs_human，不會默認當 build。
- 以上都發生在建立 worktree、job 或 model session 之前。額度資訊不參與這些判定，額度再充足也繞不過。Sized workflow 的 exact-profile qualification 是 opt-in gate：預設未啟用，以維持 #842 qualification receipt lifecycle 尚未部署時的既有派工能力。預設情況下 Manager 仍保留 sizing band，並在 `resolved_model_chain[persona].qualification` 記錄 `not-enforced`。

要啟用，operator 必須在生效 config root 的 `model-identities.yaml` host overlay 加上明確政策：

```yaml
qualification_policy:
  sized_dispatch: enforce # 可設 disabled；缺省等同 disabled
```

config root 由 `PSC_PROJECT_CONFIG_ROOT` 決定。只有 operator overlay 的 `sized_dispatch: enforce` 會啟用此 gate；缺少此區塊、設為 `disabled`、或只在 packaged identity roster 宣告，都不會啟用。啟用後，sized workflow 若缺少與 resolved key、role、完整 coverage 綁定且未撤銷的 #842 qualification，Manager 會在 spawn 前記錄 `needs_human` 並停止。Profile/quota 不會繞過人工 review 或既有 authorization/CAS。

## 持久化與升級

`WorkflowRun.execution_profile_bindings` 是 optional、versioned sibling 欄位。缺欄位代表舊版 run，讀取時保留舊 `resolved_model_chain`、attempt 與 evidence，不回填推測值或改寫歷史。新版可讀取沒有 sibling 的舊 run；舊讀取器忽略此新增 top-level 欄位。若欄位存在，格式／schema version、descriptor、profile planes、keys、persona role 與 resolved identity 必須一致；未知版本或殘缺 binding 拒收。

寫入只新增 binding，不把 profile 資料塞入 frozen chain 或歷史 receipt。Profile 更新由本次新的 dispatch decision 建立；歷史 run 不做 migration。Registry 仍使用既有 manager update 與持久化流程。

重啟語意：Manager 重啟就是重新載入 registry 與 descriptor catalog。已派出且仍在執行的 job 照舊沿用，不會重新解析 binding。重派（例如 retry-build）時，凍結的 `model_chain_override` 解析出同一個 identity；descriptor 沒變的話，binding 也逐位元組相同，#844 的 stage-execution reuse key 不會因重啟而漂移。descriptor 資料化之後，內建 adapter 產生的 binding 與原本寫死表時期逐位元組相同（由測試以固定 digest 守住）。legacy manifest 回歸測試使用 #835 合併前的 Manager 實際寫出的 registry（`tests/fixtures/execution_profile/legacy-registry-pre-835.json`），驗證 legacy 欄位位元組不變、只新增 sibling binding。

## PatchMUD producer/consumer 契約

Cortex 的 `profile_report_consumer` 是純 consumer：呼叫端必須提供預期 resolved profile key、預期 source revision 與 canonical JSON payload digest。schema version、revision、digest 或 profile key 任一不符即拒收。Cortex 不匯入 PatchMUD runtime，也不自行發布 qualification、核可 report 或改寫外部 producer。

目前自動化測試使用固定的本地 fixture；整合真實 PatchMUD #37 immutable report/fixture 時，需核對 producer revision 與其 exact serialized digest，再執行跨 repo consumer 驗收。這項測試不等同 live model、installed launcher 或真人 qualification 驗收。
