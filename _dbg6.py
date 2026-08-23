import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from tests.test_patterns import _series_with_hammer_after_downtrend
from src.analytics.patterns import analyze_candles, BULLISH_PATTERNS, BEARISH_PATTERNS

s = _series_with_hammer_after_downtrend()
cs = list(s.candles)
opens = [c.open for c in cs]; highs = [c.high for c in cs]
lows = [c.low for c in cs]; closes = [c.close for c in cs]
pats = analyze_candles(opens, highs, lows, closes)
print("patterns:", pats)
names = {p["pattern"] for p in pats}
print("names:", names)
print("bull:", bool(names & BULLISH_PATTERNS), "bear:", bool(names & BEARISH_PATTERNS))
lookback = 8
down_count = sum(1 for i in range(-lookback, 0) if closes[i] < closes[i-1])
print("down_count:", down_count, "closes tail:", [round(c,1) for c in closes[-6:]])
print("hammer body check:", abs(closes[-1]-opens[-1]), "rng:", highs[-1]-lows[-1])
