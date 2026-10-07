"""theta_option_ticket — THETA's WRITE wing: sell an XRP covered call (or buy-to-close).

This is the only place a THETA XRP call may be written. It is **armored**:

  * ``dry_run`` true (default) -> builds the ticket and stops, no network order.
  * Live is armed when EITHER:
      - env ``THETA_OPTION_LIVE = yes``  (set in the running Condor process), OR
      - the session note ``theta.option_live`` is exactly ``yes``
        (written via the Condor UI / manage_memory — scanned from the theta
        agent's memory store). This is what the strategy playbook refers to.
  * When armed, the wing submits a REAL order and ignores the ``dry_run``
    default — the arm signal IS the go. The loop's step 5 calls this routine
    every tick; it only writes when the tape printed WRITE *and* a hedge is on
    *and* the wing is armed.
  * ``amount`` must be >= the Derive option minimum (100 contracts — step 10,
    confirmed live via /public/get_instruments ``minimum_amount``). A sub-minimum
    order is refused, never sent, so a weak size cannot pretend to fill.
  * It reuses the direct-Derive v3 order path (``_theta_derive.order``): a
    trade-module SignedAction keyed by the option's base asset address and
    sub_id. ``private/order_debug`` shows the server-side hashes if it fails.

Selling an XRP call against FXRP is a COVERED call: the held FXRP offsets the
short-call liability, so this is income, not naked risk — provided the FXRP cover
is >= the sold notional (the margin guard enforces this).

Exit: buy-to-close (buy the same call back) anytime to release FXRP — no lock-up.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path as _P

from pydantic import BaseModel, Field
from telegram.ext import ContextTypes


def _theta_mod(name: str):
    path = _P(__file__).with_name(name + ".py")
    spec = importlib.util.spec_from_file_location("theta_" + name, path)
    if spec is None or spec.loader is None:
        raise ImportError(name)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CATEGORY = "Monitoring"

MIN_OPTION_CONTRACTS = 100  # XRP options: minimum_amount 100, step 10 (live-verified on Derive)


def _note_armed() -> bool:
    """Read the session note ``theta.option_live`` (set via the Condor UI /
    manage_memory) from the theta agent's memory store. No restart needed —
    the note file is scanned on every call."""
    store = _P(__file__).resolve().parents[1] / "store"  # agents/theta/store
    if not store.exists():
        return False
    # The note name is slugified on write (dots -> empty): "theta.option_live"
    # becomes "thetaoption_live". Match both forms so it works however set.
    for f in store.glob("user_*/memories/thetaoption_live.md"):
        try:
            txt = f.read_text().lower()
            if "yes" in txt.split():
                return True
        except Exception:
            pass
    for f in store.glob("user_*/memories/theta.option_live.md"):
        try:
            if "yes" in f.read_text().lower().split():
                return True
        except Exception:
            pass
    return False


def _is_armed() -> bool:
    """Wing is live when the env var OR the session note says yes."""
    if os.environ.get("THETA_OPTION_LIVE", "").strip().lower() == "yes":
        return True
    return _note_armed()


async def _auto_pick_write() -> tuple[str | None, float]:
    """Pick the current WRITE call + mark premium from theta_tape's board.

    Used when the loop calls the wing without a full ticket so the wing still
    fires on a WRITE tick. Returns (None, 0.02) on a dead board."""
    tape = _theta_mod("theta_tape")
    try:
        _spot, _funding, pick, _exp = await tape.read_board()
    except Exception:  # noqa: BLE001
        return None, 0.02
    if not pick:
        return None, 0.02
    return pick.get("instrument_name"), (tape._mark(pick) or 0.02)


class Config(BaseModel):
    instrument: str = Field(default="", description="e.g. XRP-20261030-1_4-C (copy from theta_tape)")
    amount: float = Field(default=0.0, description="contracts. Must be >= 100 for a live sell.")
    limit_price: float = Field(default=0.0, description="quote per contract. 0 => refuse (never market dump).")
    action: str = Field(default="sell", description="sell (write/open covered call) | buyback (close)")
    dry_run: bool = Field(default=True)
    gate: str = Field(default="SIT", description="SIT or WRITE from theta_tape. WRITE required to sell.")
    label: str = Field(default="theta-wing")


def _build_ticket(config: Config) -> dict:
    direction = "sell" if config.action.lower() == "sell" else "buy"
    return {
        "instrument_name": config.instrument,
        "direction": direction,
        "amount": config.amount,
        "price": config.limit_price,
        "label": config.label,
        "time_in_force": "gtc",
        "note": "covered by FXRP in theta-pm2 (PM2, XRP currency)",
    }


def _validate(config: Config) -> str | None:
    """Pure checks. Returns a refusal reason, or None if the ticket is valid."""
    if not config.instrument:
        return "REFUSED — no instrument. Copy the strike from theta_tape; never invent one."
    if "-C" not in config.instrument and "-P" not in config.instrument:
        return f"REFUSED — '{config.instrument}' is not an option instrument."
    if config.amount <= 0:
        return "REFUSED — amount must be positive."
    if config.action.lower() == "sell" and config.amount < MIN_OPTION_CONTRACTS:
        return (
            f"REFUSED — sell size {config.amount} < Derive minimum {MIN_OPTION_CONTRACTS}. "
            "A sub-minimum call cannot be covered meaningfully."
        )
    if config.limit_price <= 0:
        return "REFUSED — no limit. The clerk does not market-dump a wing."
    return None


def ticket_verdict(config: Config, live_env: str | None = None) -> str:
    """Pure gate for the wing. Never a fill here — run() owns the wire."""
    gate = (config.gate or "SIT").strip().upper()
    action = (config.action or "sell").strip().lower()
    # Selling a call only on WRITE. Buyback may run on any gate.
    if action in ("sell", "write") and gate != "WRITE":
        return f"REFUSED SIT — tape is {gate}. Wing waits; funding still prints."

    reason = _validate(config)
    if reason:
        return reason

    armed = (live_env if live_env is not None else os.environ.get("THETA_OPTION_LIVE", "")).strip().lower()
    ticket = _build_ticket(config)
    if config.dry_run:
        return f"DRY-RUN — no fill. Premium is not P&L.\n{ticket}"
    if armed != "yes":
        return (
            "REFUSED live — THETA_OPTION_LIVE is not 'yes'. "
            "Leave dry_run true; do not count premium."
        )
    # Authorized to build a real order; the actual submit happens in run().
    return f"ARMED — submitting a real {config.action} on {config.instrument}.\n{ticket}"


async def run(config: Config, context: ContextTypes.DEFAULT_TYPE) -> str:
    del context

    async def _publish(text: str, kpis: list[tuple]) -> str:
        rid = await _theta_mod("_theta_math").save_report(
            "THETA option ticket",
            "theta_option_ticket",
            kpis,
            "Dry-run by default. Premium is not P&L until a live fill.",
            rows=[
                {"Field": "Instrument", "Value": config.instrument or "—"},
                {"Field": "Action", "Value": config.action},
                {"Field": "Amount", "Value": str(config.amount)},
                {"Field": "Limit", "Value": str(config.limit_price)},
                {"Field": "Label", "Value": config.label},
                {"Field": "Dry run", "Value": "yes" if config.dry_run else "no"},
            ],
            columns=["Field", "Value"],
            section="01 / TICKET",
            section_note="Covered by FXRP in theta-pm2. No lock-up — buy-to-close anytime.",
        )
        if rid:
            text = f"{text}\n\n📊 Report: {rid}"
        return text

    kpis = [
        ("Action", config.action),
        ("Instrument", config.instrument or "—"),
        ("Amount", str(config.amount)),
        ("Dry run", "yes" if config.dry_run else "no"),
    ]
    armed = _is_armed()
    kpis.append(("Armed", "yes" if armed else "no"))
    if not armed:
        # Not armed -> describe only, never submit. (Respects dry-run default.)
        return await _publish(ticket_verdict(config, live_env="yes" if armed else None), kpis)

    # Armed: submit a real order. Auto-fill the ticket from the tape if the
    # loop called the wing without a full instrument/price (so the wing fires
    # on a WRITE tick even when the LLM passes a minimal config).
    if not config.instrument:
        name, premium = await _auto_pick_write()
        if not name:
            return await _publish(
                "ARMED but tape is SIT/dead — no WRITE instrument to sell. Wing sits.", kpis
            )
        config.instrument = name
        config.limit_price = config.limit_price or round(float(premium), 4)
    config.amount = config.amount or float(MIN_OPTION_CONTRACTS)
    # Enforce the Derive minimum even when the loop forgets to size it.
    if config.action.lower() == "sell" and config.amount < MIN_OPTION_CONTRACTS:
        config.amount = float(MIN_OPTION_CONTRACTS)

    der = _theta_mod("_theta_derive")
    try:
        creds = der.from_condor_keystore()
    except Exception as exc:  # pragma: no cover
        return await _publish(f"REFUSED live — could not load Derive creds: {exc}", kpis)

    direction = "sell" if config.action.lower() == "sell" else "buy"
    try:
        res = await der.order(
            creds,
            instrument_name=config.instrument,
            direction=direction,
            amount=config.amount,
            limit_price=config.limit_price,
            label=config.label,
        )
    except Exception as exc:
        return await _publish(f"FAILED to submit {config.action}: {exc}", kpis)
    return await _publish(f"SUBMITTED {config.action}: {res}", kpis)
