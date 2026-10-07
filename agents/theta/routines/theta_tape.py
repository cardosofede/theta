"""Read Derive's public XRP board. Print SIT or WRITE. Never place an order.

Public v3 REST only (https://api.derive.xyz/v3/public/*). This clerk does not
sign, does not POST /private/*, and does not talk to Hummingbot. A dead board
is SIT.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import aiohttp
from pydantic import BaseModel, Field
from telegram.ext import ContextTypes

import importlib.util
from pathlib import Path as _P


def _theta_mod(name: str):
    path = _P(__file__).with_name(name + ".py")
    spec = importlib.util.spec_from_file_location("theta_" + name, path)
    if spec is None or spec.loader is None:
        raise ImportError(name)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_math = _theta_mod("_theta_math")
WRITE = _math.WRITE
call_premium_pct = _math.call_premium_pct
tape_gate = _math.tape_gate
strike_from_name = _math.strike_from_name
option_kind = _math.option_kind

logger = logging.getLogger(__name__)

CATEGORY = "Market Data"

_PUBLIC = "https://api.derive.xyz/v3/public"


class Config(BaseModel):
    """Read the XRP options tape and this hour's funding."""

    currency: str = Field(default="XRP", description="Options underlying")
    perp_instrument: str = Field(
        default="XRP-PERP", description="Perp ticker for spot + funding"
    )
    write_min_pct: float = Field(
        default=0.80,
        description="WRITE when nearest OTM call mark ≥ this % of spot",
    )
    max_dte: int = Field(default=10, description="Ignore expiries beyond this many days")
    otm_min_pct: float = Field(
        default=5.0, description="Call must be at least this % above spot"
    )


async def _post(session: aiohttp.ClientSession, path: str, payload: dict) -> dict:
    url = f"{_PUBLIC}{path}"
    async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=12)) as resp:
        resp.raise_for_status()
        body = await resp.json()
        if isinstance(body, dict) and "result" in body:
            return body["result"]
        return body if isinstance(body, dict) else {}


async def _perp(session: aiohttp.ClientSession, instrument: str) -> dict:
    try:
        return await _post(session, "/get_ticker", {"instrument_name": instrument})
    except Exception as exc:  # noqa: BLE001
        logger.warning("theta_tape perp failed: %s", exc)
        return {}


def _num(ticker: dict, *keys: str) -> float:
    """First positive numeric value among ``keys`` (v3 slim tickers are strings)."""
    for key in keys:
        try:
            val = float(ticker.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if val > 0:
            return val
    return 0.0


def _spot(ticker: dict) -> float:
    """Index price, else mark (v3 slim ticker: ``I`` / ``M``)."""
    return _num(ticker, "I", "M")


def _funding(ticker: dict) -> float:
    """Hourly funding rate (v3 slim ticker: ``f``; may be negative)."""
    try:
        return float(ticker.get("f") or 0)
    except (TypeError, ValueError):
        return 0.0


async def _instruments(session: aiohttp.ClientSession, currency: str) -> list[dict]:
    """All live options for ``currency`` (``public/get_all_instruments``, paginated)."""
    out: list[dict] = []
    page, pages = 1, 1
    try:
        while page <= pages:
            data = await _post(
                session,
                "/get_all_instruments",
                {
                    "currency": currency,
                    "expired": False,
                    "instrument_type": "option",
                    "page": page,
                    "page_size": 100,
                },
            )
            out.extend(data.get("instruments") or [])
            pages = int((data.get("pagination") or {}).get("num_pages") or 1)
            page += 1
    except Exception as exc:  # noqa: BLE001
        logger.warning("theta_tape instruments failed: %s", exc)
    return out


async def _tickers(session: aiohttp.ClientSession, currency: str, expiry: str) -> list[dict]:
    """Option tickers for one expiry (``public/get_tickers`` → ``{name: slim ticker}``)."""
    try:
        data = await _post(
            session,
            "/get_tickers",
            {"currency": currency, "instrument_type": "option", "expiry_date": int(expiry)},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("theta_tape tickers %s failed: %s", expiry, exc)
        return []
    raw = data.get("tickers") or {}
    return [
        {**row, "instrument_name": name}
        for name, row in raw.items()
        if isinstance(row, dict)
    ]


def _expiry_yyyymmdd(inst: dict) -> str | None:
    details = inst.get("option_details") or {}
    exp = details.get("expiry")
    if exp is None:
        return None
    try:
        ts = float(exp)
        if ts > 1e12:
            ts = ts / 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y%m%d")
    except (TypeError, ValueError, OSError):
        return None


def _is_call(inst: dict) -> bool:
    details = inst.get("option_details") or {}
    kind = str(details.get("option_type") or inst.get("option_type") or "").lower()
    return kind in {"c", "call"} or option_kind(str(inst.get("instrument_name") or "")) == "C"


def _strike(inst: dict) -> float | None:
    details = inst.get("option_details") or {}
    raw = details.get("strike") or inst.get("strike")
    if raw is None:
        return strike_from_name(str(inst.get("instrument_name") or ""))
    try:
        val = float(str(raw).replace("_", "."))
    except (TypeError, ValueError):
        return strike_from_name(str(inst.get("instrument_name") or ""))
    return val if val > 0 else None


def _mark(ticker: dict) -> float | None:
    pricing = ticker.get("option_pricing")
    if not isinstance(pricing, dict):
        pricing = {}
    for raw in (ticker.get("M"), pricing.get("m"), ticker.get("b")):
        if raw is None:
            continue
        try:
            val = float(raw)
        except (TypeError, ValueError):
            continue
        if val >= 0:
            return val
    return None


def _pick_otm_call(tickers: list[dict], spot: float, otm_min_pct: float) -> dict | None:
    floor = spot * (1.0 + otm_min_pct / 100.0)
    scored: list[tuple[float, dict]] = []
    for t in tickers:
        if not _is_call(t):
            continue
        k = _strike(t)
        if k is None or k < floor:
            continue
        scored.append((k, t))
    if not scored:
        return None
    scored.sort(key=lambda row: row[0])
    return scored[0][1]


def _near_expiries(instruments: list[dict], max_dte: int) -> list[str]:
    """Distinct YYYYMMDD expiries of active options within ``max_dte`` days, soonest first."""
    today = datetime.now(tz=timezone.utc)
    out: set[str] = set()
    for inst in instruments:
        if not inst.get("is_active", True):
            continue
        exp = _expiry_yyyymmdd(inst)
        if not exp:
            continue
        dte = (datetime.strptime(exp, "%Y%m%d").replace(tzinfo=timezone.utc) - today).days
        if 0 <= dte <= max_dte:
            out.add(exp)
    return sorted(out)


async def read_board(
    currency: str = "XRP",
    perp_instrument: str = "XRP-PERP",
    max_dte: int = 10,
    otm_min_pct: float = 5.0,
) -> tuple[float, float, dict | None, str | None]:
    """(spot, hourly funding, nearest OTM call ticker or None, its expiry)."""
    async with aiohttp.ClientSession() as session:
        perp, instruments = await asyncio.gather(
            _perp(session, perp_instrument),
            _instruments(session, currency),
        )
        spot, funding = _spot(perp), _funding(perp)
        if spot <= 0:
            return spot, funding, None, None
        for exp in _near_expiries(instruments, max_dte):
            cand = _pick_otm_call(await _tickers(session, currency, exp), spot, otm_min_pct)
            if cand is not None:
                return spot, funding, cand, exp
    return spot, funding, None, None


async def run(config: Config, context: ContextTypes.DEFAULT_TYPE) -> str:
    """Return a one-page blotter. Never a fill."""
    del context
    spot, funding, pick, pick_exp = await read_board(
        config.currency, config.perp_instrument, config.max_dte, config.otm_min_pct
    )

    premium = call_premium_pct(_mark(pick) if pick else None, spot if spot > 0 else None)
    gate = tape_gate(premium, config.write_min_pct)
    name = (pick or {}).get("instrument_name") or "—"
    strike = _strike(pick) if pick else None

    lines = [
        "THETA tape — public board only. No order.",
        f"Spot {spot:.4f}  funding {funding * 100:.4f}%/h  gate {gate}",
        f"Week {pick_exp or 'none'}  call {name}  strike {strike if strike else '—'}",
        f"Premium {premium:.3f}% of spot" if premium is not None else "Premium none — SIT",
        f"WRITE floor {config.write_min_pct:.2f}%   SIT = wait, funding still prints",
    ]
    if gate == WRITE:
        lines.append(f"WRITE {name} — ticket is a separate clerk. Do not invent a fill.")
    else:
        lines.append("SIT — do not write.")
    text = "\n".join(lines)
    rid = await _math.save_report(
        "THETA tape",
        "theta_tape",
        [
            ("Gate", gate, None, "up" if gate == WRITE else "neutral"),
            ("Spot", f"{spot:.4f}" if spot else "—"),
            ("Funding", f"{funding * 100:.4f}%/h"),
            ("Premium", f"{premium:.3f}%" if premium is not None else "none"),
        ],
        "Public board only. No order. SIT waits; funding still prints. WRITE is a separate ticket.",
        rows=[
            {
                "Call": name,
                "Expiry": pick_exp or "none",
                "Strike": f"{strike:.2f}" if isinstance(strike, (int, float)) else "—",
                "Premium %": f"{premium:.3f}" if premium is not None else "—",
                "Floor %": f"{config.write_min_pct:.2f}",
                "Gate": gate,
            }
        ],
        columns=["Call", "Expiry", "Strike", "Premium %", "Floor %", "Gate"],
        section="01 / BOARD",
        section_note="Nearest OTM XRP call inside the DTE window.",
    )
    if rid:
        text = f"{text}\n\n📊 Report: {rid}"
    return text
