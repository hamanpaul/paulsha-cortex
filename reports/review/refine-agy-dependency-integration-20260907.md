# AGY 前置契約整合核對

本批只有進件文件／owner分拆；產品、run、installed／loaded runtime均未修正。
Root從 #852 merge `984fce5b86604aa71c6e8ecf82cc253a1441a106` 的獨立worktree整合，
不pull／reset原operator或runtime，不改任何frozen run／原evidence。

## Scope與驗收映射

- #851 唯一production model_identities.py、R1–R7；argv construction納入原smoke try，
  Exception回failed AGY、BaseException外拋、direct launch仍拒絕。真constructor、
  两種roster順序、freshcache、alias injection與zero-smoke均有oracle。
- #824 唯一production launcher.py、R1–R7；env/keyword正規化、Go整秒範圍、
  gate helper重用、所有argv與Popen轉發、非法值pre-spawn拒絕、非AGY不變；
  真非法env的direct/probe/runtime整合仍由下游candidate驗收。
- #823 從母todo移出timeout五項待辦，全部由child spec/design/tasks承接並校正
  原help短路／invalid gate語意；session/spawn/TypeError/process-group/cgroup與
  原fake-Popen／回歸仍保留。canonical work_id與session fragment不改名。
- #824 child fragment已由root正式更新遠端AC5；AC6改為no-prompt sentinel負控制。
  先讀回原body／updatedAt、保存原稿、確認未被其他writer修改才edit，再全文核對。

## Review與機械gate

Containment R1独立PASS；timeout原例外穿透MAJOR由#851前置承接、不可直接接受殘餘。
兩組fresh reader五題皆正確，核心contract未藉metadata整合改變。

| 工作 | 真完整性／sizing | 不可混淆的狀態 |
|---|---|---|
| #851 containment | complete=true；0+0+2+2+2=6Yellow | 內容review不是正式builder envelope／freeze |
| #824 timeout候選 | 有Open Questions marker：complete=false、plan blocking-decision；舊stability下4Yellow | surface-only helper仍ready/envelope bypass；不代表准入 |

Root在記憶體移除marker的對照恢復complete/ready，實際文件marker保留；
不改0/0/7宣告或公式壓分。以Tasks缺失負控制驗contract coverage；
marker之存在拒絕不能冒稱所有Tasks語意皆已產品測試。

79 runtime不讀canonical dependency自訂欄位。新work auto還需fresh confirmed authority與
`cortex:auto-on-going`，既有ongoing run的resume則先於label檢查；不能拿移除label
當全域pause。#824／#851兩issue目前labels=[]、無相關既有run。
#824 marker不能保證zero planning spawn（incomplete start可走brainstorm），所以本批
**不登錄其可claim owner、不啟auto、不start**；僅#851登錄。
父#823移除#824映射，避免未來混合派工。真正解除依賴須root驗base／loaded證據後另行
登錄、重接受與正式gate；不是請模型自動刪marker。

## Evidence與交付gate

Root再次離線跑AGY1.1.27 parser：abc／2400／2400s／9223372036s／9223372037s，
皆exit2；兩合法值只遇unknown sentinel，其他分別invalid/missing-unit/overflow。
未跑模型、未修改任何credential env或HOME；此parser證據不等live deadline。

CI policy pin為v1.0.17／`9e7fabbf0b5eea9ad933fa6798764b723934a0b7`；
本機skill source engine是release-ledger merge `b281c5da5cda0b7e3c67e148c759d99304893c56`，
root git diff確認兩者policy_check bytes相同。兩個identity仍分開記。
本PR須exact-head獨立整合review、post-commit fullpreflight、remoteCI／threads才merge；
此文件不是尚未取得的gate PASS receipt。產品Tasks全部保持未完成。
