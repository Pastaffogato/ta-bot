"""Verify pattern classifier: a CLOSED bearish candle must never yield BULL ENGULF.
Also demonstrate that a forming (temporarily bullish) candle CAN show BULL ENGULF
before closing bearish — the exact reported bug at offset > 0."""
import sys
sys.path.insert(0, ".")
from bot.patterns import classify

class B:
    def __init__(self, o, h, l, c):
        self.open, self.high, self.low, self.close = o, h, l, c

fails = []

# 1. Closed bearish candles can never be BULL ENGULF
#    Worst case: previous candle bearish, current bearish but closes above prev open
#    (even the most engulf-looking bearish-close shapes must not be bullish)
prev = B(100, 105, 95, 99)     # bearish
for o, h, l, c in [
    (102, 106, 97, 100.5),# bearish (c<o), closes above prev open
    (102, 107, 98, 101),  # bearish, engulfs prev range, closes above prev open
    (103, 108, 99, 100),  # bearish, big down move
    (95, 110, 94, 98),    # bearish, huge range
    (101, 109, 99, 100.9),# bearish, tiny body, closes above prev open
]:
    pat = classify(B(o, h, l, c), prev)
    ok = pat.label != "BULL ENGULF"
    fails.append((o, h, l, c, pat.label)) if not ok else None
    print(f"closed O{o} H{h} L{l} C{c} -> {pat.label}")

print()
# 2. Forming candle (currently up, c>o, engulfing prev open) -> BULL ENGULF fires
pat = classify(B(98, 103, 97.5, 102), prev)  # forming, temporarily bullish
print(f"forming (temporarily up) O98 H103 L97.5 C102 -> {pat.label}  <-- this is the offset>0 bug case")

print()
if fails:
    print(f"FAIL: {len(fails)} closed-bearish candles produced BULL ENGULF:", fails)
    sys.exit(1)
print("PASS: no closed bearish candle produced BULL ENGULF; bug only reproducible on a forming candle")
