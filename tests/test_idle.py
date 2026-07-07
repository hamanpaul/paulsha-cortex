import pytest

from paulsha_cortex.lib.idle import is_idle


def test_idle_when_load_below_max():
    assert is_idle(max_load=1.0, probe=lambda: (0.5, 0.0, 0.0)) is True


def test_busy_when_load_above_max():
    assert is_idle(max_load=1.0, probe=lambda: (2.0, 0.0, 0.0)) is False


def test_non_tuple_probe_rejected():
    with pytest.raises(TypeError):
        is_idle(probe=lambda: [0.5])


def test_fail_safe_on_oserror():
    def boom():
        raise OSError

    assert is_idle(probe=boom) is True
