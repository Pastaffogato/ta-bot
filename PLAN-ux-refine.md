# PLAN-ux-refine.md — Command UX Overhaul

Status: IMPLEMENT (dispatch in progress)
Audit source: deleg_9571971d (UX advisor)

## Decision log (user-confirmed)
- TP/SL signs: POSITION-RELATIVE everywhere — +N always toward profit (TP side), -N always SL side, buy AND sell, in all forms (/entry keyword path + /modify). User picked this over price-relative.
- Destructive bares (/del, /cancel, /clear): NO confirmation — solo trader, keep fast.
- Prefixless commands: YES — plain text "p bbm 1 3 5" must work like /p bbm 1 3 5. User explicitly wants no prefix at all. Solo trader, not worried about mis-sends.
- Scope: P0 bugs + P1 quick wins + P2 restructure + strict arg validation. P3 (indicator-level alerts /alert rsi 30, multi-TF /trend) SKIPPED — follow-up.

## File ownership (zero overlap)
- Agent A: `bot/app.py` ONLY (dispatcher + registrations)
- Agent B: `bot/telegram_app.py` + `bot/parsing.py` ONLY
- Lead (me): verification, AGENTS.md, memory
- Reviewer agent: post-implementation audit vs this plan

## Contract — Agent A (bot/app.py)

1. Broadened MessageHandler: replace filter `filters.TEXT & filters.Regex(r'^\.\w+')` with `filters.TEXT` on the existing dot-command handler; rename handler to `_handle_text_command`. Logic:
   - text = update.message.text.strip(); if empty → return.
   - If starts with ".": strip dot, split. cmd = parts[0].lower(). If in `_COMMANDS` → context.args = parts[1:]; await handler. Else → reply "❌ Unknown command — /help".
   - Else (plain text): ONLY if update.effective_chat.type == "private": first token lowercased; if in `_COMMANDS` → context.args = parts[1:]; await handler. Any other case → silent return. NEVER reply to non-command plain text.
   - Keep handler registered AFTER all CommandHandlers (already is — don't reorder).
2. New registrations in the handlers list: `("h", cmd_help)`, `("sig", cmd_signals)`, `("clr", cmd_clear)`, `("start", cmd_help)`. Existing loop adds each to `_COMMANDS` automatically.
3. Import list unchanged except nothing new needed (cmd_help/cmd_signals/cmd_clear already imported).

## Contract — Agent B (bot/telegram_app.py + bot/parsing.py)

### P0 bugs
1. Direction-aware TP/SL: `_resolve_tp_sl_value(val, symbol, base_price, pip_size)` → add params `direction: str, side: str` (side ∈ "tp"|"sl"). For relative (`_REL_RE`) values: buy → tp = base + N*pips, sl = base - N*pips; sell → tp = base - N*pips, sl = base + N*pips. Indicator@TF and absolute unchanged. Update BOTH call sites: cmd_entry keyword path (pass direction, side=a), cmd_modify (pass trade.direction, side="sl"/"tp"). Update docstring.
2. /data error example: `show_range` → `show_range_body`.
3. Delete the duplicate `cmd_mark_del`/`cmd_mark_list` at lines ~825-834. KEEP the ones near end of file (~1571+). Ensure no other references.

### P1 quick wins
4. /cancel: accept bare numbers and p-prefix, multiple ids: `/cancel p1 p3 7` disables each (per-id feedback lines). Bare /cancel = all (unchanged).
5. /mark del: accept M-prefix and bare numbers, multiple: `/mark del 1 3 M5`. Bare /mark del = all (unchanged).
6. /del c{id}: first arg matching `^[cC](\d+)$` → find alert via `db.get_candle_alerts(chat_id)` matching id → `db.delete_candle_alert(alert.id)`; not found → error "Candle alert cN not found". All other /del paths unchanged.
7. /list: candle alerts render as `c{id} {SYMBOL} {TF}` (e.g. `c3 XAUUSD M5`).
8. Timer-only multi-TF without focus: cmd_add — no focus AND all args parse as TF → add all as timer-only (multi). cmd_del — no focus AND all args parse as TF → delete timer-only for each. Also: in cmd_add/cmd_del symbol path, if args[0] parses as a TF but no focus set → hint error "use /fp <SYMBOL> first, or include the symbol" instead of "Symbol not found".
9. Focus persistence: `_get_focus(chat_id)` checks `_focus_pairs` dict first, then `db.get_user_prefs(chat_id).get("focus_pair")` (empty/absent → None). cmd_focus_pair set → also `db.set_user_pref(chat_id, "focus_pair", resolved)`; clear → `db.set_user_pref(chat_id, "focus_pair", "")`. Keep `_focus_pairs` as session cache.
10. /now bare default with focus: `args = ["1"]` → `args = ["5"]` (M5, matches /ind and /trend). Update usage text if it mentions M1.

### P2 restructure
11. Two-level help: cmd_help — no args → compact cheat sheet (one line per command incl. shorthand; note prefixless "p bbm 1 3 5", ".add 5" dot form, expiry suffixes 30m/2h/45s, timeframes, focus pair). `/help <topic>` (topic or its shorthand, e.g. /help p, /help price) → detailed block for that command (move the current long sections into per-command entries). Unknown topic → cheat sheet + "unknown topic". Keep parse_mode=HTML. Keep name `cmd_help`.
12. /data aliases: alias map ba→show_bid_ask, range→show_range_body, prog→show_progression, ind→show_indicators, pat→show_pattern, tr→show_trend, marks→show_marks, ohlc→show_ohlc; existing full names still accepted; `all` → every key in VALID_PREFS. `/data off all`, `/data off ind` work. Bare /data prints section state with alias shown.
13. Strict arg validation: /entry unknown token → error listing the token (was silent skip). /modify unknown token → error. parsing.py `parse_mark_args(args)` returns 4-tuple `(prices, expiry_s, expiry_label, ignored: list[str])`; cmd_mark add-path (both call sites) errors if ignored non-empty ("Invalid argument: X"). /mark del and /mark list paths unchanged.

## Verification (lead runs after review agent)
```bash
cd /c/Users/Thinkpad/Desktop/Hermes/ta-bot
.venv/Scripts/python.exe -m py_compile bot/app.py bot/telegram_app.py bot/parsing.py
.venv/Scripts/python.exe -c "import bot.app, bot.telegram_app"
# synthetic: _resolve_tp_sl_value relative signs (no MT5 call for pure-relative)
.venv/Scripts/python.exe -c "
import asyncio
from bot.telegram_app import _resolve_tp_sl_value
async def t(): return await _resolve_tp_sl_value('+20', 'XAUUSD.pc', 2400.0, 0.1, 'sell', 'tp')
print(asyncio.run(t()))  # expect 2398.0 (2400 - 20*0.1)
"
# synthetic: parse_mark_args 4-tuple
.venv/Scripts/python.exe -c "from bot.parsing import parse_mark_args; print(parse_mark_args(['2400','30m','foo']))"
# synthetic: dispatcher routing (pure logic duplicated in review)
```
Do NOT start the bot (live long-poller on same token).
