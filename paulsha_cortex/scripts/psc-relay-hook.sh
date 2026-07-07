#!/usr/bin/env bash
# Shared session_start/stop relay hook. Best-effort only: relay failures must not
# affect agent execution or completion detection.
set -u

slice="${PSC_SLICE_ID:-unknown}"
event="${PSC_RELAY_EVENT:-unknown}"
target="${PSC_RELAY_TARGET:-}"
msg="[manager] slice=${slice} event=${event}"

if [[ -n "$target" ]]; then
  printf '%s\n' "$msg" >>"$target" 2>/dev/null || true
fi

# #120 Half 1: 僅 manager 派工（launcher 注入 PSC_SLICE_ID）才推 Telegram；
# 互動 session 無 slice → no-op，避免灌爆。broadcast（不帶 --source-user-id）。
# review I-1：codex inner command 無 outer timeoutSec，故 `timeout` 設硬上限，
# 避免 reply_bridge 卡住吊死 hook（`|| true` 已吞非零，含 timeout 的 124）。
reply_bridge="${PSC_REPLY_BRIDGE:-$HOME/.agents/skills/bro/scripts/reply_bridge.py}"
if [[ "$slice" != "unknown" && -f "$reply_bridge" ]]; then
  timeout 8 python3 "$reply_bridge" --text "$msg" >/dev/null 2>&1 || true
fi

exit 0
