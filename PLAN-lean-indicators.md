# PLAN: Lean Indicator Display + Multi-TF Indicator View + Pattern Fix

**Goal:** Make the candle-alert indicator block leaner and easier to read (BB %b/Width/percentile, TR/ATR ratio, ADX value, new ER(14) + CHOP(14)), keep full detail in `/ind`, add `/indtf` (one indicator across M1-M30/H1), and eliminate false pattern recognition by making offset 0 the default.

**Stack:** Python 3.11, numpy, python-telegram-bot v20. Windows. No test suite — verification is compile + synthetic-data scripts.

---

## 1. Decision Log (from user)

- **Pattern bug root cause:** with the 8s default offset, alerts fire BEFORE the candle closes; the pattern is classified on the still-forming candle, so BULL ENGULF can be printed on a candle that later closes red. A closed bearish candle can never yield BULL ENGULF (direction requires c > o) — the bug is purely provisionality.
- **User's fix:** default offset = 0 (alerts fire at close → pattern is on the just-closed candle → final). Update `config.py` default, the user's account row in `bot.db`, and AGENTS.md.
- **BB in candle alerts:** only %b, Width (pips), Width-percentile-vs-last-100. Band prices stay in `/ind`.
- **ATR:** only TR/ATR ratio (TR of the target candle ÷ ATR14). Drop the 3-value ATR display + compressing note from alerts.
- **ADX:** value only; combine on the same line as TR/ATR.
- **ER(14) + CHOP(14):** new indicators, current value only, own line.
- **SMA50:** unchanged (line keeps SMA50 + EMA20).

## 2. Snapshot Contract (new fields — other agents depend on these names)

| field | meaning | formula | min bars |
|---|---|---|---|
| `bb_percent_b` | %b 0-100 | (close − lower) / (upper − lower) × 100 | 20 |
| `bb_width_pctile` | 0-100 | percentile rank of current band width (upper−lower) among widths of last ≤100 windows | 21+ (use available) |
| `tr_ratio` | TR/ATR | TR(last bar of selected array) / ATR14 | 15 |
| `er14` | Efficiency Ratio 0-1 | \|C_now − C_now−14\| / Σ\|ΔC\| over 14 bars | 15 closes |
| `chop14` | Choppiness 0-100 | 100·log10(ΣTR(14) / (max(high,14) − min(low,14))) / log10(14) | 14 |

NOTE: existing `bb_width_pct` is legacy-mislabeled %b and is used elsewhere — keep untouched, add `bb_percent_b` alongside.

## 3. Task Split (2 parallel agents, zero file overlap)

### Agent A — `bot/indicators.py` (sole owner)
1. `IndicatorSnapshot`: add the 5 fields above (Optional[float] = None).
2. `compute_all`: compute them, honoring `skip_current` (tr_ratio uses the last bar AFTER truncation; all arrays are chronological after internal reversal). None when bars insufficient.
3. `format_indicator_section` → lean 5 lines (see §4).
4. `format_indicator_full` (/ind): keep band prices as today, add %b + Wpct; keep ATR detail, add TR/ATR; keep ADX detail; add ER/CHOP lines.
5. `format_trend_section`: replace "ATR compressing/expanding" note with `TR/ATR {ratio:.2f}`; keep RSI part.
6. Do NOT touch `INDICATOR_TARGETS`/`resolve_indicator_target` (price levels only — ER/CHOP/ratios are NOT price targets).
7. Verify: py_compile + synthetic script (60 dummy bars, assert new fields non-None and in range, print lean section). Use `.venv\Scripts\python.exe` (numpy 1.26.4 pin). Pass `sinfo` as `types.SimpleNamespace(point=0.01, digits=2)`.

### Agent B — command layer (`bot/telegram_app.py`, `bot/app.py`) — no overlap with A
1. New `cmd_indicator_tf` (`/indtf`, alias `/itf`, dot-command `.indtf` works automatically once registered):
   - Syntax: `/indtf [SYMBOL] <indicator> [TF...]`; symbol optional if `/fp` focus set; indicator required.
   - Indicators (case-insensitive, aliases): sma50, ema20, bb / bb_b (%b), bb_width (W pips), bb_pctile (Wpct), rsi, adx, tratr / tr_atr / atr (ratio), er / er14 (×100), chop / chop14.
   - Default TFs: 1,3,5,15,30,60; trailing TF args override.
   - Per TF: `bars_n(symbol, tf, 500)` → `compute_all(bars, skip_current=False)` → extract attr → format (prices via `_fmt_ohlc`, ratios/indices 1dp).
   - Output: header `📊 XAUUSD · SMA50` + one line per TF `M1 2345.67`; skip/`—` when value is None.
2. `cmd_data`: add `show_er`, `show_chop` to `VALID_PREFS` (line ~1124).
3. Register `("indtf", cmd_indicator_tf)` + `("itf", cmd_indicator_tf)` in `app.py` handlers list.
4. Reference snapshot attrs by the contract names in §2 (Agent A lands them in parallel). Verify: py_compile + `from bot import telegram_app, app` import smoke test with venv python.

### Lead (me) — pattern fix + docs + final integration verification
- `config.py`: `DEFAULT_OFFSET_S = 8` → `0` (comment: pattern accuracy).
- `bot.db`: set user 1057071700 `default_offset_s` = 0.
- AGENTS.md: update config default, indicators module description, 5-line layout, /indtf command row, pattern pitfall note.
- Verify: classify() sanity script (closed bearish candle must NOT produce BULL ENGULF), compile all files, synthetic compute_all run, confirm no regressions in formatting/patterns imports.

## 4. Lean Candle-Alert Indicator Block (exact format)

```
Line 1 (show_bb):   BB %b 42  W 27.9p  Wpct 87
Line 2 (sma/ema):   SMA50 2345.1  EMA20 2344.8        ← unchanged
Line 3 (atr/adx):   TR/ATR 1.12  ADX 24               ← combined; show_atr/show_adx partial
Line 4 (rsi):       RSI 54.3                          ← unchanged
Line 5 (er/chop):   ER 45.2  CHOP 58.1                ← new; show_er/show_chop (default on)
```
Empty lines dropped when granular pref off (existing pattern). Full reports (/ind) keep detailed values.

## 5. Validation

- `python -m py_compile` on all changed files.
- Synthetic-bar scripts (Agent A fields; my classify check).
- Import smoke test of `bot.telegram_app` + `bot.app`.
- No live MT5 bot run during integration (same token / long-poll conflict).
