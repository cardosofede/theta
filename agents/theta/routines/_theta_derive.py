"""Thin client for Derive's v3 JSON-RPC REST API (https://docs.derive.xyz).

Used by ``theta_park`` / ``theta_margin`` to read the subaccount and by
``theta_option_ticket`` to place the covered call. Hummingbot's
``derive_perpetual`` connector is perps-only, so options go direct.

Auth (v3, https://docs.derive.xyz/authentication/session-login):
    POST https://api.derive.xyz/v3/private/<method>
    headers:
        X-DeriveWallet:     <api_key = owner EOA / multisig>
        X-DeriveTimestamp:  <ms epoch>
        X-DeriveSignature:  personal_sign(timestamp, api_secret)
    where api_secret is the wallet key or a registered session key.

Orders are EIP-712 SignedActions over the trade module
(https://docs.derive.xyz/authentication/action-signing). v3 nonces are UTC
nanoseconds sent as a string; the domain separator is Ethereum mainnet's.

No network happens at import time. Every public method is explicit about
whether it reads (safe) or writes (order).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

import threading
from decimal import ROUND_HALF_UP, Decimal

import aiohttp
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# The desk's own Derive subaccount (SM manager). Override with DERIVE_SUBACCOUNT.
THETA_DEFAULT_SUB = "70652"

# v3 mainnet. Testnet is https://testnet.api.derive.xyz/v3.
DERIVE_BASE = os.environ.get("DERIVE_API_BASE", "https://api.derive.xyz/v3").rstrip("/")

# Where a Hummingbot/Condor install stores the encrypted Derive credential,
# and the .env that holds the keystore passphrase (CONFIG_PASSWORD). theta
# reuses BOTH — the key is registered once in Condor Keys & Wallets; nobody
# copy/pastes a secret. (The perp connector cannot do spot/convert/wrap, so we
# call Derive directly with these same credentials.)
#
# Nothing here is hardcoded to one machine. Resolution order:
#   1. THETA_KEYSTORE / THETA_HB_ENV env vars (explicit override)
#   2. HUMMINGBOT_API_PATH env var (the install root)
#   3. The usual install roots under $HOME and /opt
_KEYSTORE_REL = "bots/credentials/master_account/connectors/derive_perpetual.yml"
_ENV_REL = ".env"


def _candidate_roots() -> list[Path]:
    """Install roots to search, most specific first."""
    out: list[Path] = []
    for var in ("HUMMINGBOT_API_PATH", "CONDOR_HOME"):
        v = os.environ.get(var, "").strip()
        if v:
            out.append(Path(v))
    home = Path.home()
    for name in ("hummingbot-api", "condor", "hummingbot"):
        out.append(home / name)
    out += [Path("/opt/hummingbot-api"), Path("/opt/condor")]
    seen: set[Path] = set()
    return [p for p in out if not (p in seen or seen.add(p))]


def _default_keystore() -> Path:
    override = os.environ.get("THETA_KEYSTORE", "").strip()
    if override:
        return Path(override)
    for root in _candidate_roots():
        cand = root / _KEYSTORE_REL
        if cand.exists():
            return cand
    # Nothing found: return the first candidate so the error names a real path.
    roots = _candidate_roots()
    return (roots[0] if roots else Path.home()) / _KEYSTORE_REL


def _default_hb_env() -> Path:
    override = os.environ.get("THETA_HB_ENV", "").strip()
    if override:
        return Path(override)
    for root in _candidate_roots():
        cand = root / _ENV_REL
        if cand.exists():
            return cand
    roots = _candidate_roots()
    return (roots[0] if roots else Path.home()) / _ENV_REL


# Keystore field names differ between Hummingbot connector versions. A current
# derive_perpetual connector writes wallet_address / session_private_key /
# subacct_id; older builds wrote api_key / api_secret / sub_id. Accept both.
_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "api_key": ("derive_perpetual_api_key", "derive_perpetual_wallet_address"),
    "api_secret": ("derive_perpetual_api_secret", "session_private_key"),
    "sub_id": ("sub_id", "subacct_id"),
    "account_type": ("account_type",),
}


def _find_field(text: str, names: tuple[str, ...]) -> tuple[str, str] | None:
    """Return (matched_name, hex_blob) for the first field present."""
    for name in names:
        m = re.search(rf"^{re.escape(name)}:\s*([0-9a-fA-F]+)", text, re.M)
        if m:
            return name, m.group(1)
    return None

# Derive v3 signing constants (mainnet). From docs.derive.xyz
# /migrating/breaking-changes and derive-py config/contracts.py. The domain
# separator is per chain (mainnet chainId 1); typehash and module are shared.
_PROTOCOL = {
    "trade_module_address": "0xB8D20c2B7a1Ad2EE33Bc50eF10876eD3035b5e7b",
    "domain_separator": "0xda616dfabb88681b08e1592820a41d55ddc62d68de110e327ae99d734506fe19",
    "action_typehash": "0x4d7a9f27c403ff9c0f19bce61d76d82f9aa29f8d6d4b0c5474607d9770d1af17",
}


# The options / PARK path signs Derive actions locally and needs the
# Ethereum stack. It is NOT needed for the perpetual hedge (Hummingbot signs
# that), so the import is deferred and the failure is explained, not a bare
# ModuleNotFoundError at routine-import time.
_ETH_MISSING = (
    "This routine signs Derive actions locally and needs the Ethereum stack.\n"
    "Install it with:  pip install -r requirements.txt\n"
    "(eth-account, eth-utils, eth-abi). The perpetual hedge does not need it."
)


def _require_eth_account():
    try:
        from eth_account import Account
        from eth_account.messages import encode_defunct
    except ModuleNotFoundError as exc:  # pragma: no cover - env dependent
        raise RuntimeError(_ETH_MISSING) from exc
    return Account, encode_defunct


def _keccak256(data: bytes) -> bytes:
    """Keccak-256 (NOT NIST SHA3-256 — Ethereum/Derive use legacy Keccak).

    hashlib.sha3_256 is the *different* NIST SHA3 and must NOT be used here;
    it produces wrong hashes and Derive rejects the signature (code 14014).
    eth_utils.keccak implements the correct legacy Keccak-256."""
    try:
        from eth_utils import keccak as _eth_keccak
    except ModuleNotFoundError as exc:  # pragma: no cover - env dependent
        raise RuntimeError(_ETH_MISSING) from exc
    return _eth_keccak(data)


def _hb_passphrases(hb_env: str | Path | None = None) -> list[str]:
    """Candidate keystore passphrases from Hummingbot's .env, in priority order."""
    hb_env = Path(hb_env) if hb_env is not None else _default_hb_env()
    out: list[str] = []
    for line in Path(hb_env).read_text().splitlines():
        if line.startswith("CONFIG_PASSWORD="):
            out.append(line.split("=", 1)[1].strip())
        elif line.startswith("PASSWORD="):
            out.append(line.split("=", 1)[1].strip())
    # de-dup preserving order
    seen = set()
    return [p for p in out if not (p in seen or seen.add(p))]


def _decrypt_field(blob_hex: str, passphrases: list[str]) -> str | None:
    """Decrypt one Ethereum-keystore-v3 (AES-128-CTR + PBKDF2) field.

    Tries each candidate passphrase; returns None if none match (the field
    may not be keystore-encrypted, e.g. account_type). The keystore MAC is
    keccak256(key[16:32] + ciphertext); we verify it when possible and
    otherwise fall back to a plaintext sanity check (Derive keys look like
    hex / 0x… strings).
    """
    try:
        ks = json.loads(bytes.fromhex(blob_hex))
    except (ValueError, KeyError):
        return None
    if "crypto" not in ks:
        return None
    crypto = ks["crypto"]
    ct = bytes.fromhex(crypto["ciphertext"])
    iv = bytes.fromhex(crypto["cipherparams"]["iv"])
    salt = bytes.fromhex(crypto["kdfparams"]["salt"])
    rounds = crypto["kdfparams"]["c"]
    for pw in passphrases:
        key = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, rounds, 32)
        decipher = Cipher(algorithms.AES(key[:16]), modes.CTR(iv)).decryptor()
        plain = decipher.update(ct) + decipher.finalize()
        mac = _keccak256(key[16:32] + ct).hex()
        if mac == crypto["mac"]:
            return plain.decode("utf-8")
        # Fallback: accept if the plaintext is a plausible secret (hex / 0x…).
        try:
            txt = plain.decode("utf-8")
            if txt.startswith("0x") or all(ch in "0123456789abcdefABCDEF" for ch in txt):
                return txt
        except UnicodeDecodeError:
            continue
    raise RuntimeError("keystore MAC mismatch — no candidate passphrase matched")


def from_condor_keystore(
    keystore: str | Path | None = None,
    hb_env: str | Path | None = None,
) -> dict[str, str]:
    """Reuse the Derive key already registered in Condor (Keys & Wallets).

    Decrypts ``derive_perpetual.yml`` in-process with Hummingbot's own
    ``CONFIG_PASSWORD`` — no plaintext secrets file, nothing to copy/paste.
    """
    keystore = Path(keystore) if keystore is not None else _default_keystore()
    text = Path(keystore).read_text()
    pws = _hb_passphrases(hb_env)
    if not pws:
        raise RuntimeError(
            f"No CONFIG_PASSWORD/PASSWORD found in {Path(hb_env) if hb_env else _default_hb_env()}"
        )

    out: dict[str, str] = {}
    for logical, names in _FIELD_ALIASES.items():
        found = _find_field(text, names)
        if not found:
            if logical in ("api_key", "api_secret"):
                raise ValueError(
                    f"keystore missing {logical} (tried: {', '.join(names)}) in {keystore}"
                )
            continue
        _, blob = found
        try:
            val = _decrypt_field(blob, pws)
        except RuntimeError:
            val = None
        if val is not None:
            out[logical] = val
        elif logical in ("api_key", "api_secret"):
            raise ValueError(
                f"keystore field for {logical} is not keystore-encrypted ({keystore})"
            )

    # The subaccount is the desk's own book. DERIVE_SUBACCOUNT wins; otherwise
    # use whatever the keystore carries; the constant is only a last resort.
    sub_id = (
        os.environ.get("DERIVE_SUBACCOUNT", "").strip()
        or str(out.get("sub_id") or "").strip()
        or THETA_DEFAULT_SUB
    )

    return {
        "api_key": out["api_key"],
        "api_secret": out["api_secret"],
        "sub_id": sub_id,
        "account_type": out.get("account_type", "leading"),
    }


def load_credentials(secrets_path: str | Path) -> dict[str, str]:
    """Load Derive credentials.

    Primary path: reuse the key already in Condor's keystore (no secret file).
    Fallback: a theta secrets json (``derive.json``) if you prefer that.
    """
    # Prefer the Condor keystore — that is the "register once, no copy/paste"
    # path organizers use.
    try:
        return from_condor_keystore()
    except Exception:
        pass  # fall through to the json file
    p = Path(secrets_path)
    if not p.exists():
        raise FileNotFoundError(
            f"No Derive credentials: Condor keystore absent/unreadable and "
            f"secrets file not found: {p}"
        )
    data = json.loads(p.read_text())
    key = data.get("api_key")
    secret = data.get("api_secret")
    if not key or not secret or key.startswith("YOUR_"):
        raise ValueError("derive.json is not populated (api_key/api_secret missing)")
    sub_id = (
        os.environ.get("DERIVE_SUBACCOUNT", "").strip()
        or str(data.get("sub_id") or "").strip()
        or THETA_DEFAULT_SUB
    )
    return {
        "api_key": key,
        "api_secret": secret,
        "sub_id": sub_id,
        "account_type": str(data.get("account_type") or "leading"),
    }


def _personal_sign(message: str, private_key_hex: str) -> str:
    """EIP-191 personal_sign, matching Hummingbot's derive_auth.py exactly.

    ``private_key_hex`` is the Derive api_secret (an Ethereum private key).
    Uses ``eth_account`` (see requirements.txt) so the signature is
    byte-for-byte what Derive expects: ``sign(encode_defunct(text=ts), key)``.
    """
    Account, encode_defunct = _require_eth_account()

    return Account.sign_message(
        encode_defunct(text=message), private_key=private_key_hex
    ).signature.hex()


def auth_headers(creds: dict[str, str]) -> dict[str, str]:
    """Derive v3 REST auth headers (X-DeriveWallet / X-DeriveTimestamp / X-DeriveSignature)."""
    ts = str(int(time.time() * 1000))
    return {
        "X-DeriveWallet": creds["api_key"],
        "X-DeriveTimestamp": ts,
        "X-DeriveSignature": _personal_sign(ts, creds["api_secret"]),
    }


async def _rpc(path: str, params: dict[str, Any], headers: dict[str, str] | None = None) -> Any:
    """POST one JSON-RPC call to ``{DERIVE_BASE}/{path}`` and return ``result``."""
    url = f"{DERIVE_BASE}/{path}"
    hdrs = {"accept": "application/json", "Content-Type": "application/json", **(headers or {})}
    async with aiohttp.ClientSession() as session:
        async with session.post(
            url, data=json.dumps(params), headers=hdrs,
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise RuntimeError(f"Derive {path} -> {resp.status}: {text}")
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                raise RuntimeError(f"Derive {path} bad JSON: {text}")
            if isinstance(data, dict) and data.get("error"):
                raise RuntimeError(f"Derive {path} error: {data['error']}")
            return data.get("result", data) if isinstance(data, dict) else data


async def public(method: str, params: dict[str, Any]) -> Any:
    """Unauthenticated ``public/<method>`` call."""
    return await _rpc(f"public/{method}", params)


async def _post(creds: dict[str, str], method: str, params: dict[str, Any]) -> Any:
    """Authenticated ``private/<method>`` call."""
    return await _rpc(f"private/{method}", params, auth_headers(creds))


# ── Read-only ──────────────────────────────────────────────────────────────

async def get_account_summary(creds: dict[str, str]) -> dict[str, Any]:
    """Read subaccount balances / margin via ``private/get_subaccount``.

    v3 takes only ``subaccount_id``; the wallet comes from the auth headers.
    """
    return await _post(creds, "get_subaccount", {"subaccount_id": int(creds["sub_id"])})


async def get_balances(creds: dict[str, str]) -> dict[str, float]:
    """Return {asset: amount} from the subaccount's collaterals."""
    summary = await get_account_summary(creds)
    out: dict[str, float] = {}
    if not isinstance(summary, dict):
        return out
    for row in summary.get("collaterals", []) or []:
        asset = row.get("asset_name")
        amt = row.get("amount")
        if asset and amt is not None:
            try:
                out[asset] = float(amt)
            except (TypeError, ValueError):
                continue
    return out


# ── Signing ────────────────────────────────────────────────────────────────

def _eth_account(api_secret: str):
    Account, _ = _require_eth_account()
    return Account.from_key(api_secret)


def _checksum(addr: str) -> str:
    try:
        from eth_utils import to_checksum_address
    except ModuleNotFoundError as exc:  # pragma: no cover - env dependent
        raise RuntimeError(_ETH_MISSING) from exc
    return to_checksum_address(addr)


_nonce_lock = threading.Lock()
_last_nonce = 0


def _next_nonce() -> int:
    """UTC-nanosecond nonce (v3). Strictly increasing within this process."""
    global _last_nonce
    with _nonce_lock:
        _last_nonce = max(_last_nonce + 1, time.time_ns())
        return _last_nonce


def _signed_action(creds: dict[str, str], abi_types: list[str], abi_values: list[Any]) -> dict:
    """Sign a trade-module SignedAction and return its wire fields.

    hash = keccak256(0x1901 ‖ DOMAIN_SEPARATOR ‖ keccak256(abi.encode(
        ACTION_TYPEHASH, subaccount_id, nonce, module, keccak256(data),
        expiry, owner, signer)))
    """
    try:
        from eth_abi import encode
    except ModuleNotFoundError as exc:  # pragma: no cover - env dependent
        raise RuntimeError(_ETH_MISSING) from exc

    account = _eth_account(creds["api_secret"])
    sub_id = int(creds["sub_id"])
    nonce = _next_nonce()
    expiry = int(time.time()) + 600  # v3 window: 10 s .. 120 days

    action_hash = _keccak256(
        encode(
            ["bytes32", "uint256", "uint256", "address", "bytes32", "uint256", "address", "address"],
            [
                bytes.fromhex(_PROTOCOL["action_typehash"][2:]),
                sub_id,
                nonce,
                _checksum(_PROTOCOL["trade_module_address"]),
                _keccak256(encode(abi_types, abi_values)),
                expiry,
                _checksum(creds["api_key"]),
                account.address,
            ],
        )
    )
    typed = _keccak256(b"\x19\x01" + bytes.fromhex(_PROTOCOL["domain_separator"][2:]) + action_hash)
    sig = account.unsafe_sign_hash(typed).signature
    return {
        "subaccount_id": sub_id,
        "nonce": str(nonce),
        "signer": account.address,
        "signature_expiry_sec": expiry,
        "signature": "0x" + sig.hex().removeprefix("0x"),
    }


def _decimal_str(value: Any) -> str:
    """Canonical decimal string (max 12 dp — v3 rejects finer precision)."""
    d = Decimal(str(value)).quantize(Decimal("1e-12"), rounding=ROUND_HALF_UP).normalize()
    return format(d, "f")


def _to_big(value: Any) -> int:
    """18-decimal fixed point of the same string that goes on the wire."""
    return int(Decimal(_decimal_str(value)) * 10**18)


# ── Writes ─────────────────────────────────────────────────────────────────

async def order(creds: dict[str, str], instrument_name: str, direction: str,
                amount: float, limit_price: float, order_type: str = "limit",
                time_in_force: str = "gtc", max_fee: float = 1000.0,
                label: str = "theta", debug: bool = False) -> dict[str, Any]:
    """Place an order on Derive v3 (``private/order``).

    direction: "buy" | "sell". amount is in base units (contracts for options).
    The signed TradeModuleData is (asset, sub_id, limit, amount, max_fee,
    recipient, is_bid), keyed by the instrument's base asset address and
    sub_id from ``public/get_instrument``. ``debug`` routes to
    ``private/order_debug`` which returns the server-side hashes instead.
    """
    inst = await public("get_instrument", {"instrument_name": instrument_name})
    asset_address = (inst or {}).get("base_asset_address")
    if not asset_address:
        raise RuntimeError(f"Derive has no instrument {instrument_name!r}")
    asset_sub_id = int((inst or {}).get("base_asset_sub_id") or 0)

    price, size, fee = _decimal_str(limit_price), _decimal_str(amount), _decimal_str(max_fee)
    signed = _signed_action(
        creds,
        ["address", "uint256", "int256", "int256", "uint256", "uint256", "bool"],
        [
            _checksum(asset_address),
            asset_sub_id,
            _to_big(price),
            _to_big(size),
            _to_big(fee),
            int(creds["sub_id"]),  # recipient = own subaccount
            direction == "buy",
        ],
    )
    params = {
        **signed,
        "instrument_name": instrument_name,
        "direction": direction,
        "amount": size,
        "limit_price": price,
        "max_fee": fee,
        "order_type": order_type,
        "time_in_force": time_in_force,
        "label": label,
        "mmp": False,
    }
    return await _post(creds, "order_debug" if debug else "order", params)
