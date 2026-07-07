import os
from typing import Callable, Tuple


def is_idle(max_load: float = 1.0, probe: Callable[[], Tuple[float, ...]] = os.getloadavg) -> bool:
    """Return True when system is considered idle using the 1-minute load average."""
    try:
        result = probe()
        if not isinstance(result, tuple):
            raise TypeError("probe must return a tuple like os.getloadavg()")
        load = float(result[0])
        return load <= float(max_load)
    except (OSError, AttributeError, IndexError):
        return True
