# planning-transient-classification

- **planner launcher 的暫時性服務失敗不再被判 `content` 死路**——實測（2026-08-14，run
  `workflow-88d089d71416a754dda8`）：agy 服務暫時回
  `Error: Eligibility check failed: UNAVAILABLE (code 503)`，**印錯誤文字但 exit 0**；
  launcher 信任 exit 0 去 parse stdout、找不到 JSON，失敗以
  `primary-integration-malformed: … returned no JSON object` 收場並被預設分類 `content`
  → `recover-planning` 遭 #393 fail-closed 禁止 → 一個十分鐘後自癒的 503（同一指令重跑
  即成功）成為永久死路，唯一出口 abandon。transient-誤判-死路模式第五次命中
  （#500、#507 的 content 誤分類同族）。兩項修正：
  - `planning_runtime._extract_json`：no-JSON 失敗必須帶 stdout 截斷片段——修法前錯誤
    文字隨 temp_dir 一併丟棄，operator 只看得到「no JSON object」六個字，診斷得靠手動重現。
  - `manager._is_planning_transient_service_failure`：與 #416 殘留例外同路的第二個例外判準
    ——reason 命中服務層暫時性樣態（UNAVAILABLE／503／429／rate limit／timeout／
    connection reset|refused／overloaded 等）時分類改落 `environment`，
    `_resume_decision` 因此浮現 `recover-planning`。判準刻意窄：模型「內容不從」
    （回散文不回 JSON、schema 不合、標題缺失）維持 `content` 與既有 fail-closed 意圖。
