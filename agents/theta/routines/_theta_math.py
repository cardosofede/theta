"""Pure THETA desk math. No network. No orders."""

from __future__ import annotations

WRITE = "WRITE"
SIT = "SIT"

# Official-style FXRP haircuts used for sizing (IM / MM).
FXRP_IM_HAIRCUT = 0.20
FXRP_MM_HAIRCUT = 0.18
PERP_IM_CONTINGENCY = 0.02
PERP_MM_CONTINGENCY = 0.01


def tape_gate(premium_pct: float | None, write_min_pct: float) -> str:
    """WRITE only when a real premium clears the floor. Missing → SIT."""
    if premium_pct is None:
        return SIT
    if write_min_pct <= 0:
        return SIT
    if premium_pct >= write_min_pct:
        return WRITE
    return SIT


def _option_parts(name: str) -> list[str] | None:
    """``[CCY, YYYYMMDD, STRIKE, C|P]`` from a Derive option name.

    v3 may append a risk-universe id (``XRP-20260821-1_05-C-42``); it is dropped.
    """
    parts = (name or "").split("-")
    if len(parts) == 5 and parts[4].isdigit():
        parts = parts[:4]
    if len(parts) != 4 or parts[3] not in ("C", "P"):
        return None
    return parts


def option_kind(name: str) -> str | None:
    """``"C"`` / ``"P"`` for a Derive option name, else None."""
    parts = _option_parts(name)
    return parts[3] if parts else None


def strike_from_name(name: str) -> float | None:
    """Parse Derive name ``XRP-20260821-1_05-C`` → 1.05."""
    parts = _option_parts(name)
    if parts is None:
        return None
    raw = parts[2].replace("_", ".")
    try:
        val = float(raw)
    except ValueError:
        return None
    return val if val > 0 else None


def call_premium_pct(mark: float | None, spot: float | None) -> float | None:
    """Call mark as a percent of spot. None if the board is dead."""
    if mark is None or spot is None:
        return None
    if spot <= 0 or mark < 0:
        return None
    return (mark / spot) * 100.0


def free_margin_for_short(
    fxrp_posted: float,
    usdc_sleeve: float,
    collaterals_value: float,
    locked_im: float = 0.0,
    *,
    im_haircut: float = FXRP_IM_HAIRCUT,
    safety: float = 0.90,
) -> float:
    """Free margin Derive will let a new 1x short consume.

    On Derive PM cross-margin, posted FXRP already consumes its own initial
    margin. Only what is left of the account value after every asset's IM is
    free. At 1x leverage a short needs ~100% margin, so the free dollar
    margin is the short-notional ceiling.

    `collaterals_value` is the live subaccount total; `locked_im` is margin
    already consumed by **positions and open orders** (not each collateral
    row's IM — on SM, USDC rows report IM == balance even when flat). A
    `safety` buffer is left so the create is never rejected at the edge.
    """
    fxrp = max(0.0, fxrp_posted)
    usdc = max(0.0, usdc_sleeve)
    if collaterals_value > 0:
        # locked_im == 0 is valid when flat (positions + open orders). Do not
        # fall back to per-asset IM rows — SM USDC reports IM == balance.
        free = max(0.0, collaterals_value - max(0.0, locked_im))
    else:
        free = max(0.0, fxrp * (1.0 - im_haircut) + usdc - fxrp * im_haircut)
    return max(0.0, round(free * safety, 2))


def allowed_short(
    fxrp_posted: float,
    usdc_sleeve: float,
    pile_cap: float,
    *,
    im_haircut: float = FXRP_IM_HAIRCUT,
    perp_im: float = PERP_IM_CONTINGENCY,
    sleeve_keep: float = 0.50,
) -> float:
    """Largest matching short that still leaves the sleeve mostly free.

    FXRP counts after haircut. Only (1 - sleeve_keep) of USDC may back IM.
    Result is floored at 0 and capped at pile_cap.
    """
    if pile_cap <= 0:
        return 0.0
    fxrp = max(0.0, fxrp_posted)
    usdc = max(0.0, usdc_sleeve)
    usable_collateral = fxrp * (1.0 - im_haircut) + usdc * (1.0 - sleeve_keep)
    if usable_collateral <= 0 or perp_im <= 0:
        return 0.0
    # Haircuted FXRP already "pays" most of the matched book's IM.
    # Extra cash IM is the perp contingency on the short.
    room_for_short = usable_collateral / perp_im
    sized = min(pile_cap, fxrp, room_for_short)
    return max(0.0, round(sized, 2))


def book_is_tight(
    fxrp_posted: float,
    usdc_sleeve: float,
    short_notional: float,
    *,
    mm_haircut: float = FXRP_MM_HAIRCUT,
    perp_mm: float = PERP_MM_CONTINGENCY,
    min_excess: float = 50.0,
) -> bool:
    """True when maintenance excess would sit under min_excess dollars."""
    equity = max(0.0, fxrp_posted) + max(0.0, usdc_sleeve)
    mm = max(0.0, fxrp_posted) * mm_haircut + max(0.0, short_notional) * perp_mm
    return (equity - mm) < min_excess


def pump_book(
    pile: float,
    short: float,
    move_pct: float,
    cut_pct: float,
    funding: float = 0.0,
) -> dict:
    """Pump: short is cut at cut_pct, pile keeps the whole move."""
    fxrp = pile * move_pct
    stopped = move_pct >= cut_pct
    shrt = -short * cut_pct if stopped else -short * move_pct
    net = fxrp + shrt + funding
    return {
        "fxrp": fxrp,
        "short": shrt,
        "funding": funding,
        "net": net,
        "cut": stopped,
    }


def dump_book(
    pile: float,
    short: float,
    move_pct: float,
    funding: float = 0.0,
) -> dict:
    """Dump: hedge stays. Direction cancels. Funding is the print."""
    fxrp = -pile * move_pct
    shrt = short * move_pct
    return {
        "fxrp": fxrp,
        "short": shrt,
        "funding": funding,
        "net": fxrp + shrt + funding,
        "cut": False,
    }


def quiet_funding(notional: float, rate_per_hour: float, hours: float) -> float:
    """Teaching print: notional * hourly rate * hours. Rate is a fraction."""
    if notional <= 0 or hours <= 0:
        return 0.0
    return notional * rate_per_hour * hours


def drawdown_short(base: float, drawdown_pct: float) -> float:
    """8% is scaling, not a stop. Shrink, then halt new size."""
    if drawdown_pct < 0:
        return base
    if drawdown_pct <= 0.04:
        return base
    if drawdown_pct <= 0.08:
        return round(base * 0.5, 2)
    return 0.0


def split_wallet(wallet: float) -> tuple[float, float, float]:
    """Half FXRP, half USDC sleeve, short cap = the FXRP half.

    Race is 800 → (400, 400, 400). Smoke test 60 → (30, 30, 30).
    All-USDC or a non-positive wallet is not this desk.
    """
    if wallet <= 0:
        return 0.0, 0.0, 0.0
    half = round(wallet / 2.0, 2)
    return half, half, half


def working_short(
    pile_cap: float,
    free_margin_cap: float,
    *,
    race_frac: float = 0.80,
    min_quote: float = 15.0,
) -> float:
    """Race working size: safe fraction of pile, never above free margin.

    ``race_frac`` default 0.80 leaves sleeve/IM headroom on a $400 pile
    (working $320) while still large enough for funding P&L and hedge volume.
    Below ``min_quote`` → 0 (do not open dust that fails the 10 XRP floor).
    """
    if pile_cap <= 0:
        return 0.0
    target = min(max(0.0, pile_cap) * max(0.0, race_frac), max(0.0, free_margin_cap))
    target = round(target, 2)
    return target if target >= min_quote else 0.0


def base_xrp_for_short(quote_usd: float, xrp_usd: float, *,
                     min_base: float = 10.0) -> float:
    """Base XRP for a short. Venue min is 10 XRP; round down to 0.1 step."""
    if quote_usd <= 0 or xrp_usd <= 0:
        return 0.0
    raw = quote_usd / xrp_usd
    # 0.1 step (min_base_amount_increment on Derive XRP-USDC)
    stepped = (int(raw * 10) / 10.0)
    if stepped + 1e-12 < min_base:
        return 0.0
    return round(stepped, 1)


def should_reload_hedge(
    *,
    cut_mark: float,
    spot: float,
    funding_pays_short: bool,
    rearm_band: float = 0.02,
) -> bool:
    """After a +pump cut: reload when spot is back within band of cut mark."""
    if not funding_pays_short or cut_mark <= 0 or spot <= 0:
        return False
    return spot <= cut_mark * (1.0 + rearm_band)


def should_reopen_flat(
    *,
    funding_pays_short: bool,
    flat_ticks: int,
    min_flat_ticks: int = 2,
    room_ok: bool,
    working_quote: float,
) -> bool:
    """Re-open a full working short after being flat (volume + funding hours)."""
    return (
        funding_pays_short
        and room_ok
        and working_quote > 0
        and int(flat_ticks) >= int(min_flat_ticks)
    )


async def save_report(
    title: str,
    source_name: str,
    kpis: list[tuple],
    body: str = "",
    rows: list[dict] | None = None,
    columns: list[str] | None = None,
    *,
    section: str | None = None,
    section_note: str | None = None,
    tables: list[tuple] | None = None,
) -> str:
    """Publish a Routines-tab HTML report (KPI tiles + tables, Condor Routines-tab style)."""
    try:
        from condor.reports import ReportBuilder
    except Exception:
        return ""
    builder = ReportBuilder(title)
    builder.source("routine", source_name).tags(["theta", "derive", "fxrp"])
    for item in kpis:
        label = str(item[0])
        value = str(item[1])
        delta = item[2] if len(item) > 2 else None
        trend = item[3] if len(item) > 3 else "neutral"
        builder.kpi(label, value, delta=delta, trend=trend)
    heading = section or "01 / DESK"
    builder.section(heading, section_note)
    if tables:
        for table in tables:
            t_rows, t_cols, *rest = table
            if rest:
                builder.section(str(rest[0]), rest[1] if len(rest) > 1 else None)
            if t_rows:
                builder.table(t_rows, t_cols)
    elif rows:
        builder.table(rows, columns)
    if body and not body.strip().startswith("```"):
        builder.markdown(body)
    builder.manual_order()
    return await builder.save()
