from __future__ import annotations

import tempfile
from pathlib import Path

_ROOT = Path(tempfile.mkdtemp(prefix="psc-test-homes-"))

BUILDER_HOME = str(_ROOT / "builder-home")
REVIEWER_HOME = str(_ROOT / "review-home")
GATE_HOME = str(_ROOT / "gate-home")

for _path in (BUILDER_HOME, REVIEWER_HOME, GATE_HOME):
    Path(_path).mkdir(parents=True, exist_ok=True)
