# Live Executor — Code Review & Refactor Plan

## Executive Summary
Live executor dibangun dengan pola "fire and forget" — order ditembak ke CLOB, tidak di-track, tidak di-cancel. Akibatnya: allowance habis, SELL gagal, posisi stuck. Butuh rewrite order lifecycle management.

## Root Cause Analysis

### Issue 1: SELL Failed — "not enough balance / allowance"
**Root cause:** Open GTC BUY orders mengunci USDC allowance. Saat SELL dicoba, CLOB menolak karena semua allowance sudah terpakai oleh open orders.

**Bukti dari error log:**
```
balance: 10517820, sum of active orders: 10510000, order amount: 10510000
```
Balance ($10.52) ≈ active orders ($10.51) → tidak ada room untuk order baru.

**Fix:** Cancel open orders untuk token tsb sebelum place SELL. Atau track allowance dan block entry kalau allowance < threshold.

### Issue 2: Retry Loop
**Root cause:** `check_exit()` return True setiap cycle (3s). `close_position()` gagal → return None. Cycle berikutnya: `check_exit()` True lagi → `close_position()` gagal lagi.

**Fix current (pending_close_tokens):** Workaround. Block retry tapi posisi nggak bisa close sama sekali.

**Fix proper:** Setelah close attempt (success atau fail), mark position sebagai "exiting". Jangan call check_exit lagi untuk position yg sudah "exiting". Clear status saat reconcile confirms position closed.

### Issue 3: Order Lifecycle — No Tracking
**Root cause:** `execute_entry` places order, dapat status "matched" atau "live". Kalau "live", return None (posisi tidak dibuat). Tapi order TETAP di CLOB book, mengunci allowance. Tidak ada cancel logic.

**Fix:** Track semua orders di `_pending_orders` dict. Untuk "live" orders:
- Set timeout (60s): kalau belum match, cancel
- Track order ID untuk cancel nanti
- Kalau match, create position + remove from pending

### Issue 4: Price Source — CLOB WS Only
**Root cause:** `get_price(token_id)` dari CLOB WS. Hanya 134/300 token tracked. Untuk 166 token lain, return 0 → TP/SL skip.

**Fix:** Fallback chain:
1. CLOB WS `get_price(token_id)` — real-time, 134 tokens
2. Data API `curPrice` untuk posisi yg ada — updated tiap reconcile
3. Gamma API `lastPrice` — untuk market scan
4. If all fail: skip exit check (jangan assume price=0)

### Issue 5: Zombie Positions Inflating Bankroll
**Root cause:** Reconcile creates Position objects for ALL Data API positions, including redeemable (zombie) ones. `position_repo.total_current_value()` sums ALL positions' invested values. `set_bankroll(cash + total_current_value)` inflates bankroll.

**Fix:** 
- Reconcile: DON'T create Position objects for redeemable zombies (already skipped if cur_val < 0.01, but some slip through)
- `position_repo.total_current_value()`: filter out positions with current_price <= 0.001
- Bankroll: only count positions with current_price > 0

### Issue 6: Force Mode — Uncontrolled Signal Generation
**Root cause:** Force mode generates signals for EVERY market with price in range. No rate limiting. 6+ signals fire simultaneously, each consuming allowance.

**Fix:** 
- Rate limit: max 1 signal per cycle (3s)
- Check allowance before placing order: `if available_allowance < order_usd: skip`
- Queue signals instead of firing all at once

## Refactored Architecture

```
LiveExecutor
├── OrderManager
│   ├── place_order() → track in _active_orders
│   ├── cancel_order(order_id)
│   ├── cancel_all_for_token(token_id)
│   ├── get_active_orders(token_id=None)
│   └── cleanup_filled()
├── AllowanceTracker
│   ├── get_available_usdc() → balance - sum(active orders)
│   ├── can_place_order(usd_amount) → bool
│   └── refresh() → query CLOB balance API
├── PriceFeed
│   ├── get_price(token_id) → CLOB WS → Data API → Gamma API → 0
│   └── get_position_price(pos) → current price for position
├── execute_entry(signal, bankroll)
│   ├── check allowance
│   ├── place BUY (GTC, 60s timeout)
│   ├── if matched: create Position, return
│   ├── if live: track, return None (don't block)
│   └── if timeout: cancel, return None
└── close_position(pos, price, reason)
    ├── cancel open orders for token_id
    ├── wait 2s for cancel to process
    ├── place SELL (GTC, 60s timeout)
    ├── if matched: create Trade, return
    ├── if live: mark "exiting", return None
    └── if timeout: cancel, return None
```

## Priority Order
1. **P0: Cancel open orders before SELL** — fix allowance issue
2. **P0: Price fallback** — fix TP/SL skip for untracked tokens
3. **P1: Order lifecycle tracking** — track + timeout + cancel
4. **P1: Allowance check before entry** — prevent over-allocation
5. **P2: Exit state machine** — mark "exiting" to prevent retry loop
6. **P2: Zombie guard in position_repo** — prevent bankroll inflation
7. **P3: Force mode rate limiting** — max 1 signal per cycle
8. **P3: Normal mode CLOB warmup** — use Data API prices for first 60s
