"""Black-Scholes pricing, Greeks and implied volatility for index options.

NIFTY/BANKNIFTY index options are European-style - Black-Scholes applies directly.
"""
from __future__ import annotations

import math

SQRT2 = math.sqrt(2.0)


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / SQRT2))


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def d1d2(cp: str, S: float, K: float, T: float, r: float, sigma: float):
    if sigma <= 0 or T <= 0 or S <= 0 or K <= 0:
        raise ValueError("invalid inputs S=%s K=%s T=%s sigma=%s" % (S, K, T, sigma))
    vol_sqrt_t = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / vol_sqrt_t
    d2 = d1 - vol_sqrt_t
    return d1, d2


def bs_price(cp: str, S: float, K: float, T: float, r: float, sigma: float) -> float:
    """European option price. cp: 'C' call, 'P' put."""
    d1, d2 = d1d2(cp, S, K, T, r, sigma)
    is_call = cp.upper() == "C"
    if is_call:
        return S * norm_cdf(d1) - K * math.exp(-r * T) * norm_cdf(d2)
    return K * math.exp(-r * T) * norm_cdf(-d2) - S * norm_cdf(-d1)


def bs_greeks(cp: str, S: float, K: float, T: float, r: float, sigma: float) -> dict:
    """Return delta, gamma, theta (per day), vega (per 1% IV), rho."""
    d1, d2 = d1d2(cp, S, K, T, r, sigma)
    is_call = cp.upper() == "C"
    pdf_d1 = norm_pdf(d1)
    delta = norm_cdf(d1) if is_call else -norm_cdf(-d1)
    gamma = pdf_d1 / (S * sigma * math.sqrt(T))
    theta = (
        -(S * pdf_d1 * sigma) / (2 * math.sqrt(T))
        - (r * K * math.exp(-r * T) * (norm_cdf(d2) if is_call else -norm_cdf(-d2)))
    ) / 365.0
    vega = (S * pdf_d1 * math.sqrt(T)) / 100.0
    rho = (K * T * math.exp(-r * T) * (norm_cdf(d2) if is_call else -norm_cdf(-d2))) / 100.0
    return {"delta": delta, "gamma": gamma, "theta": theta, "vega": vega, "rho": rho}


def implied_vol(
    cp: str,
    S: float,
    K: float,
    T: float,
    r: float,
    market_price: float,
    sigma0: float = 0.20,
    tol: float = 1e-7,
    max_iter: int = 120,
) -> float:
    """Newton-Raphson IV with bisection fallback. Returns NaN on failure."""
    if market_price <= 0:
        return float("nan")
    lo, hi = 0.0001, 3.0
    sigma = sigma0
    while bs_price(cp, S, K, T, r, lo) > market_price:
        lo /= 2.0
    while bs_price(cp, S, K, T, r, hi) < market_price:
        hi *= 2.0
    for _ in range(max_iter):
        price = bs_price(cp, S, K, T, r, sigma)
        diff = price - market_price
        if abs(diff) < tol:
            return sigma
        d1, _ = d1d2(cp, S, K, T, r, sigma)
        vega = S * norm_pdf(d1) * math.sqrt(T)
        if vega < 1e-10:
            break
        new_sigma = sigma - diff / vega
        if not (lo < new_sigma < hi):
            new_sigma = 0.5 * (lo + hi)
        if new_sigma > sigma:
            lo = sigma
        else:
            hi = sigma
        sigma = new_sigma
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if bs_price(cp, S, K, T, r, mid) > market_price:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi)


def expected_move(spot: float, iv_atm: float, dte_days: float, sigma_mult: float = 1.0) -> float:
    """1-sigma expected move over the remaining life of the option (approx)."""
    return spot * iv_atm * math.sqrt(max(dte_days, 0.0) / 365.0) * sigma_mult


def dte_to_years(dte_days: float) -> float:
    return max(dte_days, 0.0) / 365.0
