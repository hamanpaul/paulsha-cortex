# #835 Execution profile production 接線

沿用 execution profile schema-core，新增可信 executor adapter，並在正式 workflow identity 選擇後接入 requested／resolved／observed profile、launcher、workflow manager、generic dispatch 與 planning runtime。WorkflowRun 以 versioned sibling binding 持久化，不改 frozen model chain、attempt 或歷史 evidence；舊 run 維持 legacy 語意。

profile key 涵蓋 adapter/model/effort、loadout、toolset、sandbox、permissions、toolchain 與 requirements。dispatch／launch 在 spawn 前 fail-closed 驗證 role、pin、reviewer independence 與 Trust Root。Sized dispatch 的 exact-profile qualification 僅由 host `model-identities.yaml` overlay 明示 `qualification_policy.sized_dispatch: enforce` 啟用；預設不阻擋，並於 `resolved_model_chain` 記錄 `qualification: not-enforced`。quota 未有可信來源時明示 unknown。PatchMUD profile report consumer 驗證 schema、source revision、digest 與 exact profile key，不引入 runtime import。

同步新增 #835 AC 自動化整合測試與 README／操作文件。PatchMUD #37 真實 immutable fixture、#842 qualification lifecycle／真人核可，以及 installed／live launcher 驗收需在外部完成。
