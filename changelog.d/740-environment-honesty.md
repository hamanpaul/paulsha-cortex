# 740-environment-honesty

- **`#740` 誠實紀律補上環境維度——builder sandbox 的環境紅不再使 focused 寫入卡
  確定性自報 failed。** #738 之後 subagent-build 連兩個 job 交付合格 candidate
  （focused 綠、含 changelog）卻誠實自報 `status=failed`：builder unit 的圍堵
  （`IPAddressDeny=any`＋加固剖面）讓 network guard／egress proxy／stage9
  snapshot 六族測試**只在 builder sandbox 紅**（gate 環境全綠），而
  `status_policy` 逐字要求「If any gate failed, report failed」、#606 又禁止以
  focused 綠推定全套綠——誠實的模型別無出路，run 落 `card-terminal-explicit-stop`
  迴圈。修法：prompt 契約補 environment honesty——判準來自 Manager 在**它自己的
  gate 環境**的重跑；sandbox-only、與變更無關的失敗不構成本卡 failed，**省略**該
  gate（`authorize_terminal` 本就允許的形狀：ledger 綠即授權）、觀察寫進
  diagnostics、照常交付 candidate；宣稱該 gate 綠仍然禁止（#606 不變）、ledger
  紅照樣 fail-closed（採信端零改動、#586-B 不採）。`status_policy` 的失敗條款
  改為「because of your change」。六族 sandbox-紅測試的可攜性（受限環境具名
  skip）是 #723 一族第四例，另行處理。
