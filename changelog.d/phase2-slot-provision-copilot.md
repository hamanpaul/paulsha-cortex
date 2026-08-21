### Fixed
- **#718 repair:** systemd-template launch now pre-provisions every typed builder/reviewer writable slot before start, surfaces malformed rows with the surface id and exact slot path, and keeps write-only rows on their deployment ACL instead of the runtime-cache ACL widening path.
- **#718 repair:** downgraded Copilot template jobs now require a Manager-selected canonical OAuth `config.json`, seed a private per-job `COPILOT_HOME` under the runtime-cache slot, disable auto-update in the spec, and refuse broad GitHub token env passthrough.
