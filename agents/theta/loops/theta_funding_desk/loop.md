---
name: THETA Funding Desk
description: >-
  Parks half the wallet as FXRP, shorts a matching XRP perpetual on Derive,
  collects hourly funding (main P&L), turns the hedge on cut/reload for volume,
  and only optionally writes one call when armed — default is SIT on options.
agent_key: null
skills: []
default_config:
  frequency_sec: 300
  execution_mode: loop
  server_name: local
  total_amount_quote: 800
  race_short_frac: 0.80
  min_short_quote: 15
  min_base_xrp: 10
  reload_band: 0.02
  reopen_flat_ticks: 2
  park_auto: true
  risk_limits:
    max_position_size_quote: 320
    max_open_executors: 1
    max_drawdown_pct: 8
    max_leverage: 1
    require_triple_barrier: true
    require_trailing_stop: false
default_trading_context: 'Trade XRP-USDC on derive_perpetual. Post FXRP as margin. Leave half the wallet as USDC sleeve.'
created_by: 0
created_at: '2026-08-19T00:00:00+00:00'
---

# THETA Funding Desk

> Identity lives in `AGENT.md`. This file is the tick. **P&L = funding on a
> matched short while FXRP stays parked. Volume = real hedge fills** (open,
> pump-cut, RELOAD). Options are optional juice — default **SIT**.

Venue is `derive_perpetual`. Pair is `XRP-USDC`. One underlying only.

## Competition goal (48h)

| Axis | How this desk scores |
|---|---|
| **P&L** | Hourly funding while shorts are paid; pile kept on pumps after the +4% cut |
| **Volume** | Filled hedge opens + RELOADs after cuts (not resting order spam) |
| **Safety** | 1× only, USDC sleeve never inventoried, short ≤ 80% of pile and ≤ free margin |

Do **not** chase options premium to “make volume.” A WRITE is rare and optional.

## Locked wallet (one $800 race)

| Line | Size | Duty |
|---|---|---|
| FXRP posted | **$400** | Bank + margin. Coins stay. Never sell for cash. |
| USDC sleeve | **$400** | Liquidation / shock room. Never inventory. |
| Working XRP short | **up to $320** | 80% of pile (`race_short_frac`). Matched hedge. |
| Hard short ceiling | **$320** | `risk_limits.max_position_size_quote` — platform cap |
| Optional call | 0 or 1 | Only on WRITE + `theta.option_live=yes`. Default SIT. |

Live size is always `min(working target, theta_margin allowed short, free margin)`.
Never assume $320 if the clerk says less. Never short the full wallet.

### Why 80% / $320 (not $400)
Full $400 pile short leaves no IM headroom when FXRP haircut + perp contingency
bite together. **$320** still pays serious funding and prints real hedge volume,
with a safer path through a 48h chop. Drawdown table scales from this working base.


### XRP → FXRP mint for the $400 pile

Mint **net FXRP ≈ $400 / XRP_USD spot** (1:1), plus live mint+executor fees
(~0.1 + 0.2 XRP typical). Example at $1.50 spot: **~267 FXRP net** (~267.5 XRP
paid on XRPL). Keep **$400 USDC** sleeve separate. Derive deposit floor often
**≥ 10 FXRP**. Deposit via UI: Derive → Deposit → Flare → FXRP.

## Smoke test (small wallet)

| Line | Size |
|---|---|
| Wallet | live (e.g. ~$115 USDC-only) |
| Short | `working_short` on free margin — often ~$40–$90 until FXRP is parked |
| Min open | **10 XRP** base (~$15+) |

Pass live `wallet_usd` into `theta_margin`. Do not force race $320 on a $115 book.

## Venue floors (live-verified)

- **Min size:** 10 XRP base. Smaller → executor fails quietly.
- **MARKET / IOC:** often *no liquidity within the provided limit price*.
- **Open path:** LIMIT that can take the bid (fill), or LIMIT_MAKER to rest only
  when you intentionally wait. Proven fill path: crossing LIMIT via
  `_theta_derive.order` / fillable LIMIT on `create_position_executor`.
- Always set `entry_price`. Always `amount >= 10` after `base_xrp_for_short`.

## Barriers (this short only)

Account `max_drawdown_pct: 8` **scales size** — it is not the position stop.

```
create_position_executor(
  connector_name="derive_perpetual",
  trading_pair="XRP-USDC",
  side=2,                       # SHORT
  amount=<base XRP >= 10>,
  entry_price=<fillable limit>, # required
  leverage=1,
  stop_loss=0.04,               # +4% pump cut
  take_profit=0.50,             # dump must NOT close the hedge
  time_limit=172800,
  open_order_type=2,            # LIMIT — not MARKET
)
```

Flat kwargs only. No `triple_barrier_config` dict. No trail on the hedge.

## Drawdown response

Working base = current target (≤ $320), not a fantasy $400.

| Account DD | Short | Behavior |
|---|---|---|
| 0–4% | 100% working | Full hedge |
| 4–8% | 50% working | Half. Keep sleeve. |
| >8% | $0 new | No add. Hold or flatten if funding flipped. |

Never increase size while in drawdown. When DD heals under 4%, return to full working.

## Tick sequence (300s)

**0 — Park.** If `park_auto`, run `theta_park`.  
Read-only: reports FXRP posted vs target and the gap. Derive v3 deposits are
Ethereum L1 transactions, so posting FXRP is a manual Derive UI step — the clerk
never moves funds. Until it says **done**, size the hedge from free margin.  
Prefer a **PM** subaccount for FXRP efficiency when the house allows; SM works but haircuts harder.

**1 — Tape.** `theta_tape` → `SIT` or `WRITE`, funding %/h, nearest week call.  
Dead board → **SIT**. Funding sign drives the hedge, not the call.

**2 — Room.** `theta_margin` with live `wallet_usd`.  
Read **Allowed short** and free-margin cap. **TIGHT** → do not add.

**3 — Portfolio.** Net XRP-USDC perp on `derive_perpetual`.  
Open iff |net notional| ≥ $1.

**4 — Manage the hedge (P&L + volume).**

Compute each tick:

```
working = min(pile_cap * race_short_frac, margin.allowed_short, margin.free_cap)
working = 0 if working < min_short_quote
base = base_xrp_for_short(working, spot)   # 0 if < 10 XRP
```

| State | Action |
|---|---|
| No short, funding **pays shorts**, room OK, `base ≥ 10` | **OPEN** working short (fillable LIMIT) |
| Short on, XRP dumped | **HOLD** — do not TP |
| Short cut on +4% pump | Journal cut mark. **RELOAD** when spot ≤ cut_mark × (1 + `reload_band` 2%) **and** funding still pays |
| Flat ≥ `reopen_flat_ticks` (2), funding pays, room OK | **REOPEN** full working (volume + funding hours) |
| Funding flipped (you pay) | **FLATTEN**. Sit. No reopen until tape shows shorts paid again |
| Already one short | Never open a second |

One short max. RELOAD/REOPEN are the volume engine; funding while open is the P&L engine.

**5 — Wing (optional — default SIT).**  
You do not need options knowledge to run this race. **Skip the wing unless all hold:**

- tape **WRITE**
- hedge already on
- session memory `theta.option_live` is exactly `yes` (otherwise keep ticket dry-run)

If dry-run: journal strike only — **not** P&L. Never size the race around calls.

**6 — Journal.** funding %/h, FXRP, working target, short net, action  
(OPEN / HOLD / CUT / RELOAD / REOPEN / FLATTEN / SIT), fill yes/no.

## Open the hedge (fill checklist)

1. Live wallet + `theta_margin` → `working` quote.  
2. `base = base_xrp_for_short(working, spot)` — abort if 0.  
3. Read best bid/ask. For a **fill now**: sell limit at or through the bid (taker-style LIMIT). For **rest only**: LIMIT_MAKER above mid.  
4. `create_position_executor` as above. If MARKET-like rejection: retry once with a deeper fillable limit or `_theta_derive.order` crossing limit.  
5. Confirm |net short| ≥ $1 before counting success.  
6. Never size with `total_amount_quote/2` alone — clerk wins.

## Do not

- Do not sell FXRP for USDC to “simplify.”  
- Do not use MARKET as the primary open.  
- Do not open &lt; 10 XRP.  
- Do not short above free margin / working cap.  
- Do not write calls through Hummingbot.  
- Do not treat options WRITE as required for competition score.  
- Do not add size in drawdown.  
- Do not run two shorts.  
- Do not assume FXRP is posted until `theta_park` says **done**.

## Organizer / race boot

1. Derive `derive_perpetual` credentials on the bot account.  
2. Fund USDC sleeve; mint FXRP and deposit it in the Derive UI; confirm `theta_park` says done.  
3. Start `theta` / `theta_funding_desk` in **loop**.  
4. `python agents/theta/tests/validate_agent.py` && pure tests.  
5. First ticks: margin allowed short &gt; 0, one fillable OPEN, journal CLEAN.
