部分修正 #897(1) 與 #937(1)：候選 harvest 會在更新來源 branch 前逐檔比對 `planning_authority` pinned 檔案，只容忍 plan 類 `tasks.md`／`todo.md` 的 checkbox-only 差異；其他 bytes 差異會拒收並指出檔案與雜湊前綴，缺檔則指出路徑。#897 其餘子項與 #937 其餘子項維持待後續批次處理。
