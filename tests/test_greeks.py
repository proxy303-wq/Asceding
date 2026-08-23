import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from analytics.greeks import bs_price, bs_greeks, implied_vol, expected_move  # noqa: E402


def approx(a, b, tol=1e-6):
    assert abs(a - b) < tol, "%s != %s" % (a, b)


def test_put_call_parity():
    S, K, T, r, sigma = 23500.0, 23500.0, 7 / 365.0, 0.065, 0.13
    call = bs_price("C", S, K, T, r, sigma)
    put = bs_price("P", S, K, T, r, sigma)
    parity = call - put - (S - K * math.exp(-r * T))
    approx(parity, 0.0, 1e-8)


def test_atm_delta_around_half():
    S, K, T, r, sigma = 23500.0, 23500.0, 7 / 365.0, 0.065, 0.13
    g = bs_greeks("C", S, K, T, r, sigma)
    assert 0.48 < g["delta"] < 0.55, g
    gp = bs_greeks("P", S, K, T, r, sigma)
    # European delta parity: delta_call - delta_put = 1 exactly (K*e^(-rT) has no S-dependence)
    approx(g["delta"] - gp["delta"], 1.0, 1e-9)
    assert gp["delta"] < 0, gp


def test_gamma_positive_theta_negative():
    g = bs_greeks("C", 23500.0, 23550.0, 7 / 365.0, 0.065, 0.13)
    assert g["gamma"] > 0
    assert g["theta"] < 0
    assert g["vega"] > 0


def test_implied_vol_roundtrip():
    S, K, T, r, sigma_true = 23500.0, 23600.0, 7 / 365.0, 0.065, 0.14
    price = bs_price("C", S, K, T, r, sigma_true)
    iv = implied_vol("C", S, K, T, r, price)
    assert abs(iv - sigma_true) < 1e-4, (iv, sigma_true)


def test_expected_move_sane():
    em = expected_move(23500.0, 0.13, 7.0)
    assert 0.01 * 23500 < em < 0.05 * 23500, em


if __name__ == "__main__":
    for fn in [test_put_call_parity, test_atm_delta_around_half, test_gamma_positive_theta_negative,
               test_implied_vol_roundtrip, test_expected_move_sane]:
        fn()
        print("ok " + fn.__name__)
    print("ALL GREEK TESTS PASSED")
