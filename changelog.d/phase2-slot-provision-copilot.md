### Fixed
- **#718 repair:** systemd-template launch now pre-provisions every typed builder/reviewer writable slot before start, surfaces malformed rows with the surface id and exact slot path, and keeps write-only rows on their deployment ACL instead of the runtime-cache ACL widening path.
