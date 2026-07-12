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
#   scripts/reap-codex-brokers.sh                         # 預設 dry-run，只列出孤兒
#   scripts/reap-codex-brokers.sh --apply --cwd-root <p>  # 僅回收 <p> 底下 broker
#
# 測試 seam（單測用，平時勿設）：
#   REAP_PS_SNAPSHOT=<file>  讀檔代替 `ps`（每行："pid ppid args…"）
#   REAP_PROC_ROOT=<dir>     讀假 proc（預設 /proc）
#   REAP_KILL_CMD=<cmd>      代替 kill（注入假 killer 驗證會殺哪些 pid）
set -euo pipefail

APPLY=0
CWD_ROOT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply)
      APPLY=1
      shift
      ;;
    --dry-run)
      APPLY=0
      shift
      ;;
    --cwd-root)
      [[ $# -ge 2 ]] || { echo "--cwd-root 缺值" >&2; exit 2; }
      CWD_ROOT="$(readlink -f "$2")"
      shift 2
      ;;
    -h|--help)
      # 跳過 shebang（第 1 行），只印開頭註解區塊
      tail -n +2 "$0" | sed -n 's/^# \{0,1\}//p'
      exit 0
      ;;
    *)
      printf '未知參數: %s\n' "$1" >&2
      exit 2
      ;;
  esac
done

if [[ "$APPLY" -eq 1 && -z "$CWD_ROOT" ]]; then
  echo "--apply 需要搭配 --cwd-root" >&2
  exit 2
fi

KILL_CMD="${REAP_KILL_CMD:-kill}"
PROC_ROOT="${REAP_PROC_ROOT:-/proc}"

read_identity() {
  local pid="$1" stat_path cmdline_path cwd_path stat ppid starttime cmdline cwd
  stat_path="$PROC_ROOT/$pid/stat"
  cmdline_path="$PROC_ROOT/$pid/cmdline"
  cwd_path="$PROC_ROOT/$pid/cwd"
  [[ -r "$stat_path" && -r "$cmdline_path" && -e "$cwd_path" ]] || return 1
  stat="$(cat "$stat_path")" || return 1
  ppid="$(awk '{print $4}' <<<"$stat")"
  starttime="$(awk '{print $22}' <<<"$stat")"
  [[ -n "$ppid" && -n "$starttime" ]] || return 1
  cmdline="$(tr '\0' ' ' < "$cmdline_path" | sed 's/ $//')"
  [[ -n "$cmdline" ]] || return 1
  cwd="$(readlink -f "$cwd_path")" || return 1
  [[ -n "$cwd" && -d "$cwd" ]] || return 1
  printf '%s\t%s\t%s\t%s\n' "$ppid" "$starttime" "$cmdline" "$cwd"
}

path_within_root() {
  local path="$1" root="$2"
  [[ -n "$path" && -n "$root" ]] || return 1
  [[ "$path" == "$root" || "$path" == "$root"/* ]]
}

# 取 process 表（pid ppid args）。先整個捕捉，確保後面的 awk 自身不會混進快照（避免自我誤判）。
if [[ -n "${REAP_PS_SNAPSHOT:-}" ]]; then
  SNAP="$(cat "$REAP_PS_SNAPSHOT")"
else
  SNAP="$(ps -eo pid=,ppid=,args=)"
fi

# 找出孤兒 broker：輸出每行 "pid<TAB>snapshot_ppid<TAB>snapshot_cmdline"
mapfile -t CANDIDATES < <(printf '%s\n' "$SNAP" | awk '
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
        print b "\t" PPID[b] "\t" BROKER[b]
      }
    }
  }
' | sort -n)

declare -a ORPHANS=()
for line in "${CANDIDATES[@]}"; do
  pid="${line%%$'\t'*}"
  rest="${line#*$'\t'}"
  snapshot_ppid="${rest%%$'\t'*}"
  snapshot_cmdline="${rest#*$'\t'}"
  if ! identity="$(read_identity "$pid" 2>/dev/null)"; then
    continue
  fi
  live_ppid="${identity%%$'\t'*}"
  rest="${identity#*$'\t'}"
  snapshot_start="${rest%%$'\t'*}"
  rest="${rest#*$'\t'}"
  live_cmdline="${rest%%$'\t'*}"
  snapshot_cwd="${rest#*$'\t'}"
  [[ "$live_ppid" == "$snapshot_ppid" ]] || continue
  [[ "$live_cmdline" == "$snapshot_cmdline" ]] || continue
  if [[ -n "$CWD_ROOT" ]] && ! path_within_root "$snapshot_cwd" "$CWD_ROOT"; then
    continue
  fi
  ORPHANS+=("$pid"$'\t'"$snapshot_ppid"$'\t'"$snapshot_start"$'\t'"$snapshot_cmdline"$'\t'"$snapshot_cwd")
done

if [[ ${#ORPHANS[@]} -eq 0 ]]; then
  echo "無孤兒 codex broker。"
  exit 0
fi

printf '發現 %d 個孤兒 codex broker：\n' "${#ORPHANS[@]}"
for line in "${ORPHANS[@]}"; do
  pid="${line%%$'\t'*}"
  rest="${line#*$'\t'}"
  rest="${rest#*$'\t'}"
  rest="${rest#*$'\t'}"
  cwd="${rest#*$'\t'}"
  printf '  broker pid=%-8s cwd=%s\n' "$pid" "$cwd"
done

if [[ "$APPLY" -eq 0 ]]; then
  echo "（dry-run；加 --apply 才會 SIGTERM 回收，會 cascade 連同 codex app-server + MCP 子樹一起退）"
  exit 0
fi

echo "送 SIGTERM（graceful shutdown，cascade 整串退）："
for line in "${ORPHANS[@]}"; do
  pid="${line%%$'\t'*}"
  rest="${line#*$'\t'}"
  snapshot_ppid="${rest%%$'\t'*}"
  rest="${rest#*$'\t'}"
  snapshot_start="${rest%%$'\t'*}"
  rest="${rest#*$'\t'}"
  snapshot_cmdline="${rest%%$'\t'*}"
  snapshot_cwd="${rest#*$'\t'}"
  if ! identity="$(read_identity "$pid" 2>/dev/null)"; then
    printf '  - %s（略過：身份/工作目錄不可重驗）\n' "$pid"
    continue
  fi
  live_ppid="${identity%%$'\t'*}"
  rest="${identity#*$'\t'}"
  live_start="${rest%%$'\t'*}"
  rest="${rest#*$'\t'}"
  live_cmdline="${rest%%$'\t'*}"
  live_cwd="${rest#*$'\t'}"
  if [[ "$live_ppid" != "$snapshot_ppid" || "$live_start" != "$snapshot_start" || "$live_cmdline" != "$snapshot_cmdline" || "$live_cwd" != "$snapshot_cwd" ]]; then
    printf '  - %s（略過：身份已變更）\n' "$pid"
    continue
  fi
  if ! path_within_root "$live_cwd" "$CWD_ROOT"; then
    printf '  - %s（略過：cwd 超出 scope）\n' "$pid"
    continue
  fi
  if $KILL_CMD -TERM "$pid" 2>/dev/null; then
    printf '  ✓ %s\n' "$pid"
  else
    printf '  ✗ %s（已不存在或無權限）\n' "$pid"
  fi
done
