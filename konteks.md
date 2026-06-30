# Konteks Lengkap — PolyClaw-Cipher Live Bot

## 📋 Situasi Saat Ini

### Latar Belakang
Saya (Vox) bersama Cipher (AI agent) sedang mengembangkan bot trading Polymarket bernama **PolyClaw-Cipher v3**. Bot ini punya dua mode:
- **Paper trading** — simulasi, strategi momentum bekerja sangat baik
- **Live trading** — real money, strategi yang sama tapi eksekusi hancur

### Masalah Utama
Paper trading menghasilkan profit konsisten dengan strategi momentum. Tapi begitu dijalankan di live (real money), bot terus-terusan loss. Setelah investigasi, root cause-nya **bukan di strategi**, tapi di **infrastruktur eksekusi live** (LiveExecutor).

**Wallet real:**
- Funder address: `0xf9f38a1dc12fc665222734cf73b1a8f5daf24e9a`
- Network: Polygon (chain_id=137)
- Sig type: 3 (POLY_1271 / EIP-7702)
- Deposit: ~$9.83 USDC.e
- Current balance: ~$5 (setelah loss berturut-turut)

**Deployment:**
- VPS: AWS Lightsail Ireland (18.200.234.149)
- Container: Docker on Ubuntu
- Database: SQLite lokal per container
- Reconcile: Setiap 30 detik sync CLOB + Data API dengan local DB

### Kronologi Debugging (30 Juni 2026)
Kami debugging 3+ jam dan menemukan serangkaian masalah bertumpuk:

1. **Force mode workaround** — Normal mode momentum nggak generate sinyal karena:
   - CLOB WebSocket cold start (pct_change butuh 60 detik history)
   - Hanya 134 dari 300 token yang di-track CLOB WS
   - Price fallback tidak ada
   - Solusi: bikin "force mode" yang bypass semua filter momentum

2. **Force mode buka 6+ posisi sekaligus** — Tanpa rate limiting, sinyal ditembak semua bersamaan → allowance habis

3. **Token ID tidak di-set di force mode** — Signal.Signal object tidak punya token_id → LiveExecutor: "no token_id in signal"

4. **Sizer tidak bekerja** — Force mode hardcode $2.00/signal, sizer 17% tidak jalan karena `max_capital_pct` di per-strategy config = 0.45 (45%), bukan 0.17

5. **TP/SL tidak trigger** — Reconcile membuat ulang Position object setelah restart, tapi tidak memanggil `strategy.register_entry()` → `_entry_prices` kosong → `check_exit()` selalu return False

6. **TP/SL detected tapi SELL gagal** — Setelah TP/SL terdeteksi, `close_position()` memanggil CLOB API untuk SELL. Gagal dengan error:
   ```
   not enough balance / allowance: balance=10517820, sum of active orders=10510000, order amount=10510000
   ```
   Root cause: Open GTC BUY orders dari force mode masih live di CLOB book, ngunci semua allowance USDC.

7. **Retry loop** — Setelah SELL gagal, `check_exit()` return True lagi 3 detik kemudian → coba SELL lagi → gagal lagi → loop forever

8. **Workaround retry loop** — Bikin `_pending_close_tokens` guard untuk mencegah retry. Tapi ini bikin posisi nggak bisa close sama sekali.

9. **Zombie positions inflate bankroll** — 6 posisi lama (resolved markets) dengan nilai $0 masih di Data API. Reconcile tidak exclude dari `total_invested_cost` → bankroll dashboard naik palsu jadi $40+

10. **Dashboard bankroll tidak sinkron** — position_repo.total_current_value() include zombie → set_bankroll() inflate

### Attempts & Patches (semua sudah ada di kode repo ini)
- ✅ Auto-register entry untuk reconcile positions
- ✅ Pending close token guard (live SELL) + allowance error guard
- ✅ Reconcile clear hanya untuk token yang tidak ada di Data API
- ✅ _manage_positions skip untuk pending close tokens
- ✅ Dashboard flash animation + live counter
- ✅ TG /portfolio + CLOB balance real-time
- ✅ Sizer 17% via max_capital_pct
- ✅ Token ID di force mode Signal
- ❌ Semua patch di atas = band-aid. Root cause (order lifecycle) tetap tidak terselesaikan.

---

## 🔍 Root Cause Analysis

### Masalah #1: Order Lifecycle — Tidak Ada Tracking
**File:** `src/polyclaw_cipher_v3/execution/live.py`

Executor menembak order ke CLOB dengan pola "fire and forget":
- `execute_entry()` → place BUY (GTC) → dapat status "matched" atau "live"
- Kalau "live" (limit order on book), return None. Tapi order TETAP di CLOB book.
- Tidak ada tracking order ID, tidak ada cancel logic, tidak ada timeout.
- Akibat: open orders menumpuk, allowance USDC terkunci total.

### Masalah #2: SELL Gagal Karena Allowance
Saat TP/SL ingin close posisi:
- `close_position()` langsung place SELL order
- Tapi open BUY orders untuk token yang sama masih live di book
- CLOB menolak karena semua allowance sudah terpakai
- Tidak ada logic untuk cancel open orders dulu sebelum SELL

### Masalah #3: Price Source — CLOB WS Hanya 134/300 Token
- `_manage_positions()` pakai `self.clob_feed.get_price(pos.token_id)`
- Untuk 166 token yang tidak di-track → return 0 → `if current > 0` skip → TP/SL tidak dicek
- Tidak ada fallback ke Data API curPrice atau Gamma API lastPrice

### Masalah #4: Zombie Positions
- 6 posisi resolved (Brazil, Wimbledon, Germany matches) masih muncul di Data API dengan currentValue=$0
- Reconcile tetap menghitung `total_invested_cost` mereka → cost_equity inflated
- position_repo menyimpan semua posisi termasuk zombie → total_current_value() inflated

### Masalah #5: Force Mode Tanpa Rate Limiting
- `momentum.py` force mode: setiap market dengan harga valid = sinyal
- Tidak ada rate limit, tidak ada queue, tidak ada allowance check
- 6+ sinyal ditembak bersamaan → allowance habis → posisi baru gagal

---

## 📂 File Kunci untuk Review

| File | Deskripsi | ⭐ Priority |
|---|---|---|
| `src/.../execution/live.py` | LiveExecutor — execute_entry, close_position, allowance | 🔴 P0 |
| `src/.../execution/reconcile.py` | CLOB + Data API sync, zombie handling | 🔴 P0 |
| `src/.../bot.py` (line 549-650) | _manage_positions — TP/SL, stale close | 🔴 P0 |
| `src/.../strategy/momentum.py` | Force mode, normal mode bugs | 🟡 P1 |
| `src/.../core/clob_ws.py` | CLOB WebSocket — price source, pct_change | 🟡 P1 |
| `src/.../risk/sizer.py` | Position sizing logic | 🟢 P2 |
| `config/live.yaml` | Live bot configuration | 🟢 P2 |
| `CODE_REVIEW_RESULT.md` | Pre-review analysis + proposed architecture | 📄 Ref |

---

## 🎯 Permintaan untuk Reviewer

### Yang Kami Butuhkan
1. **Review kode secara menyeluruh** — Baca file-file kunci di atas. Identifikasi semua masalah, bukan cuma yang sudah kami catat.
2. **Refactor plan untuk LiveExecutor** — Kami butuh arsitektur baru untuk order lifecycle management. Lihat proposal awal di `CODE_REVIEW_RESULT.md`.
3. **Prioritas perbaikan** — Mana yang paling kritis untuk diselesaikan dulu.
4. **Code examples** — Kalau bisa, berikan contoh kode konkret untuk fix-fix utama.

### Konteks Teknis
- **CLOB V2 API:** Polymarket menggunakan CLOB (Central Limit Order Book) dengan REST API + WebSocket
- **py-clob-client-v2:** Python SDK untuk CLOB. Ada di PyPI.
- **Order types:** GTC (Good Till Cancelled) — tidak ada IOC/FOK
- **Order status:** "matched" (filled) atau "live" (on book)
- **Minimum order:** 5 shares
- **Balance API:** GET /balance-allowance — returns free USDC balance (tidak termasuk yang di-lock open orders)
- **Signature type:** 3 (POLY_1271, EIP-7702 compatible)
- **USDC.e decimals:** 6 (raw balance / 1,000,000 = USD)
- **Data API:** `data-api.polymarket.com/positions` — returns posisi dengan curPrice dari CLOB mid
- **Gamma API:** `gamma-api.polymarket.com/markets` — market metadata

### Constraints
- Paper trading harus TETAP jalan (jangan rusak paper executor)
- Live executor adalah plugin/replacement untuk paper executor (implement BaseExecutor interface)
- Bot running di Docker container, single process, async (asyncio)
- Tidak boleh tambah dependency besar tanpa alasan kuat

### Yang SUDAH berfungsi (jangan diubah kecuali perlu)
- Paper executor (`execution/paper.py`) — tidak ada masalah
- CLOB WebSocket feed — 134 tokens tracked, data real-time
- Strategy momentum (NORMAL mode) — logic benar, cuma blocked by infrastructure issues
- TG bot (`scripts/tg_live_bot.py`) — command handler, portfolio check
- Risk manager + sizer — sizing logic OK
- Reconcile basic flow — CLOB + Data API sync basic OK

### Yang PERLU di-refactor total
- **LiveExecutor** (`execution/live.py`) — order lifecycle, allowance management, close flow
- **_manage_positions** (`bot.py` line ~549) — price source, exit state machine
- **Reconcile zombie handling** (`execution/reconcile.py`) — exclude $0 positions
- **Force mode** (`strategy/momentum.py`) — rate limiting, proper signal generation

---

## 🧪 Cara Test Setelah Refactor

```bash
# 1. Set environment
cp .env.example .env
# Edit .env dengan:
#   PRIVATE_KEY=<live wallet private key>
#   POLYMARKET_API_KEY=<l2 api key>
#   POLYMARKET_API_SECRET=<l2 api secret>  
#   POLYMARKET_API_PASSPHRASE=<l2 api passphrase>
#   LIVE_FUNDER=0xf9f38a1dc12fc665222734cf73b1a8f5daf24e9a

# 2. Build & run
docker compose -f docker-compose.live.yaml up --build

# 3. Monitor
curl http://localhost:8090/api/stats
docker logs polyclaw-live -f
```

**Expected behavior setelah fix:**
- Maksimal 4 posisi terbuka bersamaan
- Setiap posisi ≤ 17% bankroll ($1.50-1.70 untuk BR $10)
- TP +8% dan SL -4% terdeteksi dan dieksekusi dengan benar
- SELL order sukses (tidak kena allowance error)
- Tidak ada retry loop
- Bankroll dashboard akurat (tidak inflate oleh zombie)
- Harga untuk TP/SL selalu tersedia (fallback kalau CLOB WS tidak ada data)
