# Cortex 動態派工討論語彙

本文件補充 Cortex 現有語彙，供 2026-09-07 精修實作與評測契約使用；既有 repo CONTEXT.md 定義持續適用。Work Item、WorkflowRun、Persona、Facet 與 Independence domain 沿用 repo 定義。

## Language

**Executor（執行器）**：承接任務並操作模型與工具的 agent runtime，例如一種 CLI agent；同一執行器可以提供多個模型。
_Avoid_: 將 agent runtime、模型及 Persona 視為同一件事。

**Model identity（模型身分）**：執行器提供的一個可辨識模型選項；它本身不代表已具備特定角色能力或有剩餘額度。
_Avoid_: 註冊即合格、模型名稱即能力保證。

**Execution profile（執行配置）**：一次評測或執行所實際採用的執行器版本、模型版本、effort、工具及隔離條件的組合。
_Avoid_: 全系統固定模型組合、只以模型名稱代表實驗條件。

**Effort setting（推理設定）**：某個模型與執行器原生支援的推理投入選項；不同產品的同名檔位不保證等價。
_Avoid_: 跨模型通用的 high／max 數值尺度。

**Task demand（任務需求）**：完成一個工作步驟所需的角色能力、品質、工具、獨立性、資源與時間條件。
_Avoid_: 任務需求直接等同某個固定模型名稱。

**Qualification evidence（資格證據）**：針對特定任務類別及執行配置，支持其通過品質或相容性門檻的可追溯證據。
_Avoid_: 候選宣告、probe 成功、額度足夠。

**Operational track record（實務紀錄）**：實際工作中觀測到的結果、用量與耗時紀錄；它的任務分布與量測條件有別於標準評測。
_Avoid_: 將生產成功率直接改寫成 PatchMUD 排名。

**Quota pool（額度池）**：由同一額度規則約束的一群使用者、工作或執行配置；可同時有短期、長期、模型專屬及帳號共用等多重限制。
_Avoid_: executor×model 即完整的額度邊界。

**Quota observation（額度觀測）**：某個來源在特定時間對額度池餘量、限制或重置時間的觀測，可能完整、部分或未知。
_Avoid_: 剩餘 token 保證、沒有資料即額度為零或無限。

**Usage forecast（用量預估）**：針對任務與執行配置的資源需求估計，包含不確定性與適用範圍。
_Avoid_: 已發生的用量、帳單、provider 保證值。

**Budget reservation（額度預留）**：在派出任務前，為其預期需求保留的可用額度份額，避免受管並行工作重複使用同一份餘量。
_Avoid_: provider 已扣款、外部工作絕不會消耗此額度。

**Selection preference（選擇偏好）**：在滿足硬性要求的候選中，調整優先順序的操作者意圖，例如偏好某個供應商。
_Avoid_: 無法 fallback 的固定綁定。

**Explicit pin（明確綁定）**：操作者要求本次工作使用特定執行配置的硬性限制；無法滿足時必須明示等待或阻塞。
_Avoid_: 將一般偏好解讀成禁止 fallback，或把明確綁定靜默降級。

**Selection decision（派工決策）**：針對一個步驟，根據當時需求、候選資格、額度及政策所作的可重讀選擇。
_Avoid_: 永久 model chain、事後由 Persona 猜測執行者。

**Fallback（候選切換）**：當原選擇無法滿足執行條件時，改用另一個仍符合任務及政策要求的配置。
_Avoid_: 換模型名稱便算避開同一額度池、為了有模型可用而降低品質或隔離門檻。
