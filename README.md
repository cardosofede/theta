# THETA

**Hodl the coins. Hedge the dump. Earn when Derive pays.**

Principal carry desk on **Derive** — not a forecast bot.  
Park wrapped XRP (FXRP) as margin, short a matching **XRP-USDC** perpetual so a dump cannot empty the wallet, and take the **hourly funding** print while that hedge is on. When the options tape says the week is expensive, optionally sell **one** covered call. When the week is cheap, **SIT**. Funding still prints either way.

One book. One underlying. Two tickets at most.

| | |
|---|---|
| **Category** | Principal carry · hourly funding · options-aware wing |
| **House** | Derive only (`derive_perpetual` + isolated options ticket) |
| **Pair** | XRP-USDC |
| **Cadence** | Condor tick every **300s** |
| **Race envelope** | **$800** (half wallet working, half sleeve) |

---

## Why it exists

Idle XRP earns nothing. Posted FXRP on Derive can collect a rate **without selling the coins**. A matching **1×** perpetual short covers the dump. A small pump stop (**+4%**) takes the short off so the pile can run. If shorts start paying, the desk flattens and sits.

Three holder verbs (same as the deck cover):

| Verb | Duty |
|---|---|
| **HODL / PARK** | FXRP stays posted as bank **and** margin. Never sell the pile for cash. |
| **HEDGE** | Matching XRP perpetual short. Dump stays hedged. Pump cut at +4% so the pile can run. |
| **EARN / COLLECT** | Hourly funding is the paycheck. One real call only on **WRITE**. |

THETA’s edge on Derive: **one wrapped asset (FXRP) is simultaneously the hold and the margin** — so the spot+perp carry needs no second pile, and the same book can also sell a call.

---

## Funding the desk — wrapping XRP into FXRP

THETA's margin is **FXRP**, wrapped XRP on Flare. Everything the desk does starts here, so the on-ramp ships with the agent rather than living in someone's shell history.

**Working code:** [`tools/fxrp_onramp/`](tools/fxrp_onramp/) — dry-run by default, key-free, resolves the Core Vault and every fee live from the Flare registry.

```
XRPL XRP  ──Payment + 32-byte memo──▶  FAssets Core Vault
                                              │
                                              ▼
                                    FXRP on Flare (chain 14)
                                              │
                                     Derive UI: Deposit → Flare → FXRP
                                              │
                                              ▼
                              THETA subaccount margin  ──▶  theta_park
```

### How to do it

```bash
cd tools/fxrp_onramp && npm install
npm run preflight                                    # read-only: vault, token, fees
cp .env.example .env                                 # fill in recipient + amount
node mint_fxrp.mjs --recipient 0x<flare-eoa> --amount 30            # dry run
node mint_fxrp.mjs --recipient 0x<flare-eoa> --amount 30 --broadcast
```

Then move it onto Derive: **Derive → Deposit → Flare → FXRP**.

### What the mint costs

The XRPL payment is `net + max(net × feeBIPS / 10000, minimumFee) + executorFee`. Read live on mainnet:

| Setting | Value |
|---|---|
| `getDirectMintingFeeBIPS` | 10 (0.10%) |
| `getDirectMintingMinimumFeeUBA` | 0.1 XRP |
| `getDirectMintingExecutorFeeUBA` | 0.2 XRP |

The minimum binds at small sizes, so converting **30 XRP costs 30.30 XRP** — not 30.03. `npm run preflight` always prints current values; nothing is hardcoded.

### Two things that will cost you money

- **Mint only to a Flare EOA whose private key you hold.** The mint recipient is not the same thing as a Derive address. Minting to a contract-wallet address strands the FXRP — it is a contract on Derive (chain 957) and a *keyless* EOA on Flare (chain 14), so no session key can ever spend it. `mint_fxrp.mjs` refuses one known address of exactly this kind.
- **The Derive hop is manual, and cannot be automated.** Flare is chain **14**, Derive is chain **957**, and FXRP is a different contract on each. Derive v3 removed the `private/deposit` RPC — deposits are on-chain transactions (see [Depositing](https://docs.derive.xyz/getting-started/depositing)) and there is no supported programmatic bridge from Flare. This is the one manual step in THETA's funding path, and the reason `theta_park` only checks the posted FXRP and never self-funds.

The mint is also not instant: `tesSUCCESS` on XRPL means the payment landed, then Flare executors finalise it. Poll the FXRP balance; do not re-send.

---

