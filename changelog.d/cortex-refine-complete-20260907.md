# 十四類 Cortex 精修計畫與 P1 進件

- 以 #829 列管 R01–R14 修正／驗收與 B0–B6 依賴，保留 PatchMUD #37 外部 producer gate。
- 明定可擴充 executor/model/native effort、profile-aware qualification、多池 quota observation、任務 forecast、atomic reservation 與安全 fallback。
- 校正 P1 文件的 history API、損毀 backoff、failure consumer、session/cgroup 與 bounded refresh 契約，將 bucket-C 校正併回 #496／#497。
- 保留既有 work_id；只註冊已具 P1 issue/todo 的工作，#828 與下游工作不混入本次進件。
- 本 fragment 為規劃／進件交付，不代表相應 code、runtime 或完整生命周期已修正。
