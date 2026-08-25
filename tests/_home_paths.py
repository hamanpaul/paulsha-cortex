from __future__ import annotations

import tempfile
from pathlib import Path

# Keep the TemporaryDirectory object alive for the whole test process so its
# finalizer removes the shared fixture tree instead of leaking /tmp entries.
_ROOT_TEMPORARY_DIRECTORY = tempfile.TemporaryDirectory(prefix="psc-test-homes-")
_ROOT = Path(_ROOT_TEMPORARY_DIRECTORY.name)

BUILDER_HOME = str(_ROOT / "builder-home")
REVIEWER_HOME = str(_ROOT / "review-home")
GATE_HOME = str(_ROOT / "gate-home")

for _path in (BUILDER_HOME, REVIEWER_HOME, GATE_HOME):
    Path(_path).mkdir(parents=True, exist_ok=True)

_HOME_STAT = Path(BUILDER_HOME).stat()
FAKE_ACCOUNT_IDS = (_HOME_STAT.st_uid, frozenset({_HOME_STAT.st_gid}))


def fake_account_ids(_account: str) -> tuple[int, frozenset[int]]:
    return FAKE_ACCOUNT_IDS
