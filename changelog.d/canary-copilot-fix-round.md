### Fixed

- **#716 canary 的 Copilot findings 修正回合**：deployment canary 碰到 `delivery-needs-human`／`copilot-findings` 時，不再直接判失敗，而是走 operator 的正式出口 `retry-build --expected-candidate <candidate>`（#1139／#1206），把 findings 交給 builder 修正，最多 2 回合。這段期間 Copilot 雖三度給出「Approval recommended」，但每次都附一條 optional finding（todo 漏勾、Purpose 佔位、docstring），ship 因此停下；canary 若要求首輪零 finding，就無法判斷部署本身是否可用（#716）。
