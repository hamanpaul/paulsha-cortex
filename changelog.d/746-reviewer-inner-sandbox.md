# 746-reviewer-inner-sandbox

- **`#746` claude reviewer 的內層 sandbox 依 runner mode 分岔——verification 卡在
  三分部署下終於能實跑命令**（#714 的 reviewer lane 版）。claude Bash 工具的內層
  sandbox 是 bubblewrap，與模板 unit 加固剖面硬性互斥（`bwrap: Can't read
  /proc/sys/kernel/overflowuid`；`failIfUnavailable: true` 使 8/8 命令全滅，模型
  誠實 needs_human）。permgen 已量過保留 bwrap 要付四條放寬、其中兩條（user
  namespace／mount）是外層加固面存在的理由——0819 對 codex 的裁決因此是「換掉
  內層形態」（#716 B）。本票同型：direct（單 UID）逐字維持內層 sandbox（唯一
  邊界、零回歸）；systemd 模板（三分）`sandbox: {enabled: false}`，邊界由既有
  機制承擔——外層 26 項加固＋egress 白名單（#725）、candidate 竄改於採信端
  fail-closed（#650 `require_candidate_unchanged`）、reviewer 對來源樹零可達
  （#641）。`permissions.deny` 的憑證／HOME 讀取拒絕兩模式逐字相同。planner 的
  claude job 走 `--tools ""` 從未啟動 bwrap，故此牆藏到 reviewer lane 首跑才露出。
