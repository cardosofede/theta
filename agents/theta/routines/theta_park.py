"""PARK check: is enough FXRP posted as margin, and how much is still missing?

Runs as theta's step 0 when ``park_auto`` is on. Read-only: it reads the
subaccount's collaterals via Derive's v3 API (see _theta_derive) and reports
the gap against the target, leaving the USDC sleeve per the desk's split rule.

Derive v3 removed the ``private/deposit`` RPC — deposits are Ethereum L1
calls (https://docs.derive.xyz/getting-started/depositing), so posting FXRP
is a manual step in the Derive UI (see README "Funding the desk").
Credentials come from the Condor keystore, with ``secrets/derive.json`` as a
fallback.

No order is placed here. This is setup, not the hedge.
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path as _P
from pydantic import BaseModel, Field
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

CATEGORY = "Setup"

_THIS = _P(__file__).resolve().parent
_SECRETS = _THIS / "secrets" / "derive.json"


def _theta_mod(name: str):
    path = _P(__file__).with_name(name + ".py")
    spec = importlib.util.spec_from_file_location("theta_" + name, path)
    if spec is None or spec.loader is None:
        raise ImportError(name)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Config(BaseModel):
    """Check posted FXRP against the PARK target. Read-only."""

    enabled: bool = Field(default=True, description="Run the PARK check")
    wallet_usd: float = Field(default=0.0, description="If >0, split half FXRP / half sleeve. 0 = manual legs")
    fxrp_target: float = Field(default=400.0, description="FXRP to post (quote). Ignored if wallet_usd>0")
    usdc_sleeve: float = Field(default=400.0, description="USDC to leave as sleeve. Ignored if wallet_usd>0")
    # NOTE: the field name must not look like a credential. Condor refuses to
    # publish a Config whose field name matches its credential pattern
    # (SEC-617) — the default would be readable by every user of the install.
    creds_file: str = Field(
        default="",
        description="Optional path to derive.json. Empty = use the Condor keystore.",
    )


async def run(config: Config, context: ContextTypes.DEFAULT_TYPE) -> str:
    del context

    async def _out(lines: list[str], kpis: list[tuple]) -> str:
        text = "\n".join(lines)
        try:
            rid = await _theta_mod("_theta_math").save_report(
                "THETA park",
                "theta_park",
                kpis,
                "Setup only. No hedge is opened here.",
                rows=[{"Line": str(k[0]), "Value": str(k[1])} for k in kpis],
                columns=["Line", "Value"],
                section="01 / COLLATERAL",
                section_note="FXRP posted as margin on the desk subaccount.",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("theta_park report failed: %s", exc)
            return text
        if rid:
            text = f"{text}\n\n📊 Report: {rid}"
        return text

    if not config.enabled:
        return await _out(
            ["THETA park — disabled. FXRP must be posted manually."],
            [("PARK", "disabled")],
        )

    d = _theta_mod("_theta_derive")
    try:
        creds = d.load_credentials(config.creds_file or _SECRETS)
    except (FileNotFoundError, ValueError) as exc:
        return await _out(
            [
                "THETA park — BLOCKED: derive.json not ready.",
                f"  {exc}",
                "  Register the Derive key in Condor Keys & Wallets (or set creds_file).",
            ],
            [("PARK", "blocked")],
        )

    if config.wallet_usd > 0:
        fxrp_target = round(config.wallet_usd / 2.0, 2)
        usdc_sleeve = fxrp_target
    else:
        fxrp_target = config.fxrp_target
        usdc_sleeve = config.usdc_sleeve

    try:
        balances = await d.get_balances(creds)
    except Exception as exc:  # noqa: BLE001
        return await _out(
            [f"THETA park — read failed: {exc}"],
            [("PARK", "read failed")],
        )

    usdc = balances.get("USDC", 0.0)
    fxrp = balances.get("FXRP", 0.0)
    xrp = balances.get("XRP", 0.0)
    kpis = [
        ("FXRP posted", f"{fxrp:.2f}"),
        ("USDC", f"{usdc:.2f}"),
        ("Target FXRP", f"{fxrp_target:.2f}"),
        ("Sleeve", f"{usdc_sleeve:.2f}"),
    ]
    lines = [
        "THETA park — numbers only.",
        f"USDC {usdc:.2f}  XRP {xrp:.2f}  FXRP posted {fxrp:.2f}",
        f"Target FXRP {fxrp_target:.2f}  sleeve {usdc_sleeve:.2f}",
    ]

    if fxrp >= fxrp_target - 1.0:
        lines.append(
            f"FXRP already posted ({fxrp:.2f} >= {fxrp_target:.2f}). PARK done — no action."
        )
        return await _out(lines, kpis + [("PARK", "done", None, "up")])

    need_fxrp = round(fxrp_target - fxrp, 2)
    spare_usdc = round(max(0.0, usdc - usdc_sleeve), 2)
    lines.append(
        f"PARK pending: post {need_fxrp:.2f} more FXRP (spare USDC above sleeve {spare_usdc:.2f})."
    )
    lines.append(
        "Derive v3 deposits are Ethereum L1 transactions — deposit FXRP in the Derive UI. "
        "Hedge sizes from free margin until then."
    )
    return await _out(lines, kpis + [("PARK", "pending", None, "down")])
