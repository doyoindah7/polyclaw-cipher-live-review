# PolyClaw-Cipher Live Bot — Code Review

## What This Is
Live trading executor for Polymarket prediction markets. Uses CLOB V2 API.

**Status:** Broken. Stop-loss detection works but SELL execution fails with allowance errors. Need full refactor of order lifecycle management.

## Quick Start (for reviewers)
```bash
git clone <this-repo>
cd polyclaw-live-review
pip install -e .
pip install py-clob-client-v2 web3 eth-abi
```

## Key Files for Review

### ⭐ Core Issue — Live Executor
`src/polyclaw_cipher_v3/execution/live.py` — The main problem. Order lifecycle not managed.

### ⭐ TP/SL & Position Management
`src/polyclaw_cipher_v3/bot.py` — `_manage_positions()` method (line ~549)

### ⭐ State Reconciliation
`src/polyclaw_cipher_v3/execution/reconcile.py` — Syncs CLOB state with local DB

### ⭐ Strategy (Force Mode)
`src/polyclaw_cipher_v3/strategy/momentum.py` — Force mode workaround (line ~91)

### Configuration
`config/live.yaml` — Live bot config (17% sizer, force mode enabled)

### Infrastructure
- `Dockerfile` — Container build
- `docker-compose.live.yaml` — Live bot deployment
- `scripts/tg_live_bot.py` — Telegram command handler

## Known Issues (see CODE_REVIEW_RESULT.md for full analysis)
1. **CLOB allowance exhaustion** — GTC BUY orders lock USDC, SELL fails
2. **No order lifecycle** — orders placed, never tracked or cancelled
3. **Price source gap** — CLOB WS only tracks 134/300 tokens
4. **Zombie positions** — Inflate bankroll calculations
5. **Force mode** — Uncontrolled signal generation, no rate limiting

## Architecture
```
PolyClawBot (bot.py)
├── Market Scanner → Gamma API
├── CLOB WebSocket → Real-time orderbook (134 tokens)
├── Strategies → Momentum (force mode)
├── Live Executor (live.py) ← BROKEN
│   ├── execute_entry() → BUY via CLOB V2
│   ├── close_position() → SELL via CLOB V2
│   └── get_clob_balance() → USDC balance
├── Reconcile (reconcile.py)
│   └── Sync CLOB + Data API with local DB
├── Risk Manager
│   └── Sizer (17% per trade)
└── Telegram Bot (scripts/tg_live_bot.py)
```

## Wallet
- Funder: `0xf9f38a1dc12fc665222734cf73b1a8f5daf24e9a`
- Network: Polygon (chain_id=137)
- Auth: Signature type 3 (POLY_1271/EIP-7702)
- L2 API key: configured via env vars
