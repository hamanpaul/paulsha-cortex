# #987 main-probe conflict classification

- `git merge-tree --write-tree --name-only --no-messages -z` 在 exit `1`、
  tree OID 有效且 conflicted path 清單為空時，現在會維持 generic
  `conflict` 分類，不再誤判成 `multiple-conflicts`；既有的
  `CHANGELOG.md` 頂端插入特例、單一路徑非 changelog conflict、多重
  conflict path，以及 clean-behind／in-sync／failure 行為維持不變。
