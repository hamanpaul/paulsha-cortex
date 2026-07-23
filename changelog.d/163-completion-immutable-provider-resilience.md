fix(workflow): 已完成 run 的 CompletionRecord 一旦落盤即固定重用既有有效紀錄，不再因 authority source revision 漂移被隔離；缺失或損毀的 completion 檔也只會跳過單列，不再讓整個 workflow provider degraded。
