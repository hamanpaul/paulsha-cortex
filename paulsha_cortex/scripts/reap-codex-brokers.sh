#!/usr/bin/env bash
# reap-codex-brokers.sh — 回收孤兒 codex app-server-broker（多 worktree 派工後殘留）
#
# 背景：Claude Code 的 codex 外掛為每個 worktree 派工起一個常駐 broker
#   (.claude/plugins/cache/openai-codex/codex/<ver>/scripts/app-server-broker.mjs serve …)。
#   broker 底下掛 codex app-server + 一整套 codex 自己的 MCP（memory / sequential-thinking /
#   mcp-lsp …）。母 session 退出時沒人收 → 整串被 init/systemd 收養成孤兒常駐，吃 RAM。
#
# 偵測：args 含 `app-server-broker.mjs serve`，且其 parent 為 reaper（init / systemd / pid 1）
#   = 孤兒。parent 為活 claude（或任何非 reaper 行程）的 broker = 某 session 正在用，一律跳過。
#
# 回收：broker 內建 graceful shutdown（SIGTERM / SIGINT → 關 appClient、關 socket、unlink
#   socket+pidfile），對 broker pid 送 SIGTERM 即 cascade 整串退。本腳本只送 SIGTERM，不用 -9。
#
# 用法：
#   scripts/reap-codex-brokers.sh            # 預設 dry-run，只列出孤兒
#   scripts/reap-codex-brokers.sh --apply    # 實際送 SIGTERM 回收
#
# 測試 seam（單測用，平時勿設）：
#   REAP_PS_SNAPSHOT=<file>  讀檔代替 `ps`（每行："pid ppid args…"）
#   REAP_KILL_CMD=<cmd>      代替 kill（注入假 killer 驗證會殺哪些 pid）
set -euo pipefail

APPLY=0
for arg in "$@"; do
  case "$arg" in
    --apply)   APPLY=1 ;;
    --dry-run) APPLY=0 ;;
    -h|--help)
      # 跳過 shebang（第 1 行），只印開頭註解區塊
      tail -n +2 "$0" | sed -n 's/^# \{0,1\}//p'
      exit 0 ;;
    *)
      printf '未知參數: %s\n' "$arg" >&2
      exit 2 ;;
  esac
done

KILL_CMD="${REAP_KILL_CMD:-kill}"

# 取 process 表（pid ppid args）。先整個捕捉，確保後面的 awk 自身不會混進快照（避免自我誤判）。
if [[ -n "${REAP_PS_SNAPSHOT:-}" ]]; then
  SNAP="$(cat "$REAP_PS_SNAPSHOT")"
else
  SNAP="$(ps -eo pid=,ppid=,args=)"
fi

# 找出孤兒 broker：輸出每行 "pid<TAB>cwd"
mapfile -t ORPHANS < <(printf '%s\n' "$SNAP" | awk '
  {
    pid = $1; ppid = $2
    args = ""
    for (i = 3; i <= NF; i++) args = args (i > 3 ? " " : "") $i
    PPID[pid] = ppid
    # reaper：取 args 第一個 token 的 basename，為 init / systemd 即視為收割者
    #   （涵蓋 /sbin/init、WSL /init 子收割鏈、systemd --user 子收割，以及 PID 1）。
    #   以 exe basename 比對，避免 args 任意處含 "systemd"/"init" 字樣的行程被誤判成 reaper。
    exe = args; sub(/ .*/, "", exe); sub(/.*\//, "", exe)
    if (pid == 1 || exe == "init" || exe == "systemd") REAPER[pid] = 1
    if (args ~ /app-server-broker\.mjs serve/) BROKER[pid] = args
  }
  END {
    for (b in BROKER) {
      if (PPID[b] in REAPER) {
        cwd = "?"
        if (match(BROKER[b], /--cwd [^ ]+/)) cwd = substr(BROKER[b], RSTART + 6, RLENGTH - 6)
        print b "\t" cwd
      }
    }
  }
' | sort -n)

if [[ ${#ORPHANS[@]} -eq 0 ]]; then
  echo "無孤兒 codex broker。"
  exit 0
fi

printf '發現 %d 個孤兒 codex broker：\n' "${#ORPHANS[@]}"
for line in "${ORPHANS[@]}"; do
  pid="${line%%$'\t'*}"; cwd="${line#*$'\t'}"
  printf '  broker pid=%-8s cwd=%s\n' "$pid" "$cwd"
done

if [[ "$APPLY" -eq 0 ]]; then
  echo "（dry-run；加 --apply 才會 SIGTERM 回收，會 cascade 連同 codex app-server + MCP 子樹一起退）"
  exit 0
fi

echo "送 SIGTERM（graceful shutdown，cascade 整串退）："
for line in "${ORPHANS[@]}"; do
  pid="${line%%$'\t'*}"
  if $KILL_CMD -TERM "$pid" 2>/dev/null; then
    printf '  ✓ %s\n' "$pid"
  else
    printf '  ✗ %s（已不存在或無權限）\n' "$pid"
  fi
done
