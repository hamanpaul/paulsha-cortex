# Trust-root Phase 2 runtime harvest fix

- Preserve the exact systemd template instance when harvesting isolated Codex
  runtime credentials, with regression coverage for suffixed registry ids.
- Keep raw job-id credential harvesting separate from exact persisted-instance
  joining; missing or malformed template authority now fails closed.
