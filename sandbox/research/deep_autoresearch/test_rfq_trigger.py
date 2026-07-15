import importlib.util
from pathlib import Path


path = Path(__file__).with_name("rfq_trigger.py")
spec = importlib.util.spec_from_file_location("rfq_trigger", path)
mod = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(mod)


def test_kaplan_meier_with_censoring():
    rows = mod.kaplan_meier([1, 2, 2, 3], [True, True, False, True])
    assert rows[0] == (1.0, 0.75, 1, 0)
    assert rows[1][0] == 2.0 and rows[1][2:] == (1, 1)
    # The final remaining subject dies at t=3, so survival reaches zero.
    assert abs(rows[-1][1]) < 1e-12


def test_ccdf_is_monotone():
    x, y = mod.downsample_ccdf([1, 1, 2, 10], points=10)
    assert x == sorted(x)
    assert len(x) == len(set(x))
    assert all(a >= b for a, b in zip(y, y[1:]))
    assert y[0] == 1.0 and y[-1] == 0.25
