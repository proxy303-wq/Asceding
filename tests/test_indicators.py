import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import numpy as np
from analytics.indicators import sma, ema, rsi, atr, adx, vwap, linear_slope  # noqa: E402


def test_sma():
    x = [1.0, 2.0, 3.0, 4.0, 5.0]
    out = sma(x, 3)
    assert math.isnan(out[1])
    assert abs(out[2] - 2.0) < 1e-9
    assert abs(out[4] - 4.0) < 1e-9


def test_ema_converges_to_mean():
    x = [10.0] * 50
    out = ema(x, 5)
    assert abs(out[-1] - 10.0) < 1e-9


def test_rsi_bounds_and_extremes():
    up = [float(i) for i in range(1, 31)]
    assert abs(rsi(up, 14)[-1] - 100.0) < 1e-9
    dn = [float(30 - i) for i in range(30)]
    assert abs(rsi(dn, 14)[-1]) < 1e-9
    osc = [math.sin(i / 2.0) * 10 + 100 for i in range(60)]
    r = rsi(osc, 14)
    assert not np.isnan(r[-1])
    assert 0 <= r[-1] <= 100


def test_atr_positive():
    h = [100 + math.sin(i) * 5 for i in range(40)]
    l = [100 + math.sin(i) * 5 - 3 for i in range(40)]
    c = [100 + math.sin(i) * 5 - 1 for i in range(40)]
    a = atr(h, l, c, 14)
    assert not np.isnan(a[-1])
    assert a[-1] > 0


def test_adx_bounds():
    h = [100 + i * 0.5 for i in range(60)]
    l = [99 + i * 0.5 for i in range(60)]
    c = [99.5 + i * 0.5 for i in range(60)]
    d = adx(h, l, c, 14)
    assert not np.isnan(d[-1])
    assert 0 <= d[-1] <= 100


def test_vwap():
    t = [10.0, 20.0, 30.0]
    v = [1.0, 1.0, 1.0]
    vw = vwap(t, v)
    assert abs(vw[-1] - 20.0) < 1e-9


def test_slope_sign():
    y = [float(i) for i in range(30)]
    s = linear_slope(y, 10)
    assert s[-1] > 0


if __name__ == "__main__":
    for fn in [test_sma, test_ema_converges_to_mean, test_rsi_bounds_and_extremes, test_atr_positive,
               test_adx_bounds, test_vwap, test_slope_sign]:
        fn()
        print("ok " + fn.__name__)
    print("ALL INDICATOR TESTS PASSED")
