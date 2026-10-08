# #1368 清除 README 懸空引用

README 中 repo 內檔案引用改為從 repo 根目錄起算的完整路徑；runtime、operator config 與 receipt 檔名所在行加上 `<!-- doc-drift-ignore -->`。文件內容意義維持不變，README 不再產生 R-22 懸空引用。
