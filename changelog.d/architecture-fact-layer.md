---
type: docs
---
- 將架構候選修正為 source-pinned workflow-review/v1 HTML：16 個元件、22 條具名關係、4 種角色、7 個階段、4 條恢復情境及 5 種狀態模型；補上 Persona／Monitor 和 controller/store/observer 邊界。
- 重算真實 source excerpt hashes、補 unknowns/conflicts，移除 Registry 派工的錯誤關係；README 增加 HTML 入口，附 source／embedded-data／browser regression gates。不宣稱 runtime E2E 或人類驗收已完成。
- 全套 CI checkout 保留 pinned source commit 供證據驗證；修正 coverage shadow CLI 測試的固定資料／真實時鐘不一致，並以時鐘前進後確實清掃到期紀錄的負控制保留 TTL 行為驗證。不修改 runtime 或跳過測試。
