"""Derive v3 wire-format tests for THETA. No network. No orders.

Pins the v3 constants and request shapes from https://docs.derive.xyz
(/migrating/breaking-changes, /authentication/action-signing) so a drift
back to the v2 API fails here instead of with a live 14014.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

ROUTINES = Path(__file__).resolve().parents[1] / "routines"
sys.path.insert(0, str(ROUTINES))

from _theta_math import option_kind, strike_from_name  # noqa: E402


def _load(name: str):
    spec = importlib.util.spec_from_file_location("test_" + name, ROUTINES / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


derive = _load("_theta_derive")
tape = _load("theta_tape")

# Throwaway keys — never funded, never registered.
OWNER = "0x000000000000000000000000000000000000dEaD"
SESSION_KEY = "0x" + "11" * 32
CREDS = {"api_key": OWNER, "api_secret": SESSION_KEY, "sub_id": "70652"}


def test_v3_base_and_headers():
    assert derive.DERIVE_BASE == "https://api.derive.xyz/v3"
    h = derive.auth_headers(CREDS)
    assert set(h) == {"X-DeriveWallet", "X-DeriveTimestamp", "X-DeriveSignature"}
    assert h["X-DeriveWallet"] == OWNER
    assert len(h["X-DeriveTimestamp"]) == 13  # ms epoch


def test_domain_separator_is_mainnet_chain_1():
    from eth_abi import encode
    from eth_utils import keccak

    domain = keccak(
        encode(
            ["bytes32", "bytes32", "bytes32", "uint256", "address"],
            [
                keccak(b"EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"),
                keccak(b"Matching"),
                keccak(b"1.0"),
                1,
                "0xeB8d770ec18DB98Db922E9D83260A585b9F0DeAD",
            ],
        )
    )
    assert "0x" + domain.hex() == derive._PROTOCOL["domain_separator"]


def test_nonce_is_nanoseconds_and_increasing():
    a, b = derive._next_nonce(), derive._next_nonce()
    assert len(str(a)) == 19
    assert b > a


def test_decimal_wire_matches_signed_big_int():
    assert derive._decimal_str(0.0035) == "0.0035"
    assert derive._decimal_str(100) == "100"
    assert derive._to_big("0.0035") == 3_500_000_000_000_000
    # v3 rejects >12 dp; we round to 12 so string and big-int agree.
    assert derive._decimal_str("0.1234567890123") == "0.123456789012"


def test_order_body_is_v3_and_signature_recovers():
    from eth_account import Account

    sent: dict = {}

    async def fake_public(method, params):
        assert method == "get_instrument"
        return {
            "instrument_name": params["instrument_name"],
            "base_asset_address": "0xC58947c13bef1eFA75259D7F685f77DAd7dfD979",
            "base_asset_sub_id": "123456789",
        }

    async def fake_post(creds, method, params):
        sent["method"], sent["params"] = method, params
        return {"order": {"order_id": "x"}}

    derive.public, derive._post = fake_public, fake_post
    asyncio.run(derive.order(CREDS, "XRP-20261030-1_6-C", "sell", 100, 0.0035, label="t"))

    p = sent["params"]
    assert sent["method"] == "order"
    required = {
        "subaccount_id", "instrument_name", "direction", "amount", "limit_price",
        "max_fee", "nonce", "signer", "signature", "signature_expiry_sec",
    }
    assert required <= set(p)
    # v2-only fields must be gone.
    assert not {"asset_address", "sub_id", "type", "is_bid", "recipient_id"} & set(p)
    assert isinstance(p["nonce"], str) and len(p["nonce"]) == 19
    assert p["subaccount_id"] == 70652
    assert (p["amount"], p["limit_price"]) == ("100", "0.0035")

    # Rebuild the typed-data hash independently and recover the signer.
    from eth_abi import encode
    from eth_utils import keccak

    data = encode(
        ["address", "uint256", "int256", "int256", "uint256", "uint256", "bool"],
        [
            "0xC58947c13bef1eFA75259D7F685f77DAd7dfD979", 123456789,
            3_500_000_000_000_000, 100 * 10**18, 1000 * 10**18, 70652, False,
        ],
    )
    action = keccak(
        encode(
            ["bytes32", "uint256", "uint256", "address", "bytes32", "uint256", "address", "address"],
            [
                bytes.fromhex(derive._PROTOCOL["action_typehash"][2:]), 70652, int(p["nonce"]),
                derive._PROTOCOL["trade_module_address"], keccak(data),
                p["signature_expiry_sec"], OWNER, p["signer"],
            ],
        )
    )
    typed = keccak(b"\x19\x01" + bytes.fromhex(derive._PROTOCOL["domain_separator"][2:]) + action)
    assert Account._recover_hash(typed, signature=p["signature"]) == Account.from_key(SESSION_KEY).address


def test_get_subaccount_sends_only_subaccount_id():
    sent: dict = {}

    async def fake_post(creds, method, params):
        sent["method"], sent["params"] = method, params
        return {"collaterals": [{"asset_name": "FXRP", "amount": "267.5"}]}

    derive._post = fake_post
    assert asyncio.run(derive.get_balances(CREDS)) == {"FXRP": 267.5}
    assert sent == {"method": "get_subaccount", "params": {"subaccount_id": 70652}}


def test_slim_ticker_spot_and_funding():
    live = {"I": "1.46233", "M": "1.46116", "f": "0.000012500", "option_pricing": None}
    assert tape._spot(live) == 1.46233
    assert tape._spot({"M": "1.4"}) == 1.4
    assert tape._spot({}) == 0.0
    assert tape._funding(live) == 0.0000125
    assert tape._funding({"f": "-0.0001"}) == -0.0001
    assert tape._mark({"option_pricing": {"m": "0.0035"}}) == 0.0035


def test_tickers_and_instruments_v3_shapes():
    calls: list = []

    async def fake_post(session, path, payload):
        calls.append((path, payload))
        if path == "/get_tickers":
            return {"tickers": {"XRP-20261030-1_6-C": {"M": "0.0035"}}}
        page = payload["page"]
        return {
            "instruments": [{"instrument_name": f"p{page}"}],
            "pagination": {"count": 2, "num_pages": 2},
        }

    tape._post = fake_post
    rows = asyncio.run(tape._tickers(None, "XRP", "20261030"))
    assert rows == [{"M": "0.0035", "instrument_name": "XRP-20261030-1_6-C"}]
    assert calls[0][1]["expiry_date"] == 20261030 and "expired" not in calls[0][1]

    insts = asyncio.run(tape._instruments(None, "XRP"))
    assert [i["instrument_name"] for i in insts] == ["p1", "p2"]
    assert all(path == "/get_all_instruments" for path, _ in calls[1:])


def test_option_names_with_risk_universe_suffix():
    assert strike_from_name("XRP-20261030-1_6-C-42") == 1.6
    assert option_kind("XRP-20261030-1_6-C-42") == "C"
    assert option_kind("XRP-20261030-1_6-P") == "P"
    assert option_kind("XRP-PERP") is None
    assert tape._is_call({"instrument_name": "XRP-20261030-1_6-C-42"})
