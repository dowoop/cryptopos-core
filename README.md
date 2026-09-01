# cryptopos-core

A dependency-free payment-rail kernel for Python. Install blockchain payments
into a web app, ERP, terminal, bot, game, or service without importing a host
framework. A rail declares a concrete network and asset, then independently
reports whether it can validate recipients, build payer instructions, observe
transfers, and settle them.

## Status — read this before you take money with it

**Not audited.** This library has been reviewed by its author and by adversarial
AI review. No external security audit has been performed, and it has never
handled mainnet funds.

**What has been proven, and how.** Every rail below has settled real testnet
money in the parent project, where these adapters shipped built into core. As of
2026-08-31 one rail has also settled real money through the *published* packages
— installed as wheels, resolved through the `cryptopos.rails` entry point,
exactly as a stranger's host would:

| rail package | settled through the published wheel |
|---|---|
| `cryptopos-rail-ootle` | **yes** — 3,141,592 µXTR, tx `d661a4399f3afe5bc77e0f8e03e8245a2a653eaded5d17475c93953f1090d720` |
| `cryptopos-rail-bitcoin` | not yet |
| `cryptopos-rail-evm` | not yet |

A Solana devnet rail exists and has settled real testnet money, but it has
**not** been extracted into a package here and there is no
`cryptopos-rail-solana` to install. It is named only so that its absence is
deliberate rather than an omission a reader has to discover.

That distinction is not pedantry. This project has four recorded incidents where
a suite was fully green while the deployed code could not take a payment — a
required protocol field that made an installed wheel undriveable, a timezone
conversion that made every payment look late, a test fixture that agreed with
the defect it was meant to catch, and an adapter asked with another chain's RPC
method. **A green suite is not evidence that a rail works.**

## Capability, and what `chargeable` does not mean

`chargeable=True` means a rail declares all four capabilities: it can validate a
recipient, build a payment request, observe the chain and return a settlement
decision. It does **not** mean the payment is bound to the sale. Read each rail
package's binding section; they differ enormously, and the weakest is the
default on four of the seven rails.

Built around refusal at money boundaries: the calls that decide where funds go
refuse rather than guess. See the [security model](#security-model) for exactly
what is verified and what is not.

The core is pure standard library with zero dependencies. Independently
installed rail plugins may own transport dependencies the core should not
force on every application.

```bash
pip install cryptopos-core
```

## Installable payment rails

**Core drives no rails of its own.** `register_builtins()` registers the six
*request-only* catalog entries — the chains this package can describe and build
a payment request for, and cannot observe or settle. Every drivable rail is an
installed package discovered through the `cryptopos.rails` entry-point group:
`cryptopos-rail-bitcoin`, `cryptopos-rail-evm`, `cryptopos-rail-ootle`. Asking
the registry for one without installing it raises `RailNotInstalled`, which is
the honest answer rather than a stub.

```bash
pip install cryptopos-core cryptopos-rail-bitcoin
```

```python
from cryptopos_core.plugin import PaymentIntent
from cryptopos_core.registry import RailRegistry

registry = RailRegistry()
registry.register_builtins()   # six request-only catalog rails
registry.discover()            # the installed rail packages, by entry point

rail = registry.get("bitcoin:testnet4/native:btc")
configuration = {"endpoint": "https://mempool.space/testnet4/api"}

readiness = rail.readiness(configuration)
if not readiness.chargeable:
    raise RuntimeError(readiness.unavailable)

# Capture provider facts before exposing the payment request. Bitcoin's
# built-in rail also proves the address has no prior transaction history.
baseline = rail.capture_baseline("tb1q...", configuration)
intent = PaymentIntent(
    intent_id="invoice-1042",
    rail_key=rail.key,
    recipient="tb1q...",
    amount_native=125_000,
    created_at_epoch=1_787_100_000,
    expires_at_epoch=1_787_101_800,
    baseline=baseline,
)

request = rail.create_request(intent)       # exact BIP-21 URI

# Provider reads are bounded. EVM rails return a cumulative batch and resume
# from it until the provider tip has been covered; Bitcoin completes in one read.
observations = rail.observe(intent, configuration)
while not observations.complete:
    observations = rail.observe(intent, configuration, observations)

decision = rail.settle(intent, observations, claimed_transaction_ids=frozenset())
# Persist every credited ID atomically with the settlement decision.
credited_transaction_ids = decision.transaction_ids
```

Pass only an incomplete batch back to `observe`. Once a batch is complete,
start the next scheduled observation cycle without it so the rail revalidates
the current canonical chain rather than carrying an old snapshot forward.

Hosts own storage, scheduling, authorization, exchange-rate policy, and the
invoice state machine. Plugins perform bounded operations and return immutable
facts. Third-party packages register rails through the `cryptopos.rails`
entry-point group; `RailRegistry.discover()` loads them explicitly, with no
import-time network access.

Private keys, transaction signing, refunds, payouts, custody, and treasury
movement are outside this contract. They have a different threat model from
receiving and proving a payment and should not become an extra capability on a
merchant's read-only rail by accident.

`readiness.chargeable` means all four charge capabilities passed their
deployment checks. EVM readiness exercises the actual full-block or token-log
method the rail needs, not only `eth_chainId`; it still cannot prove that a
provider is honest. It is intentionally stricter than “a QR can be built.”
`cryptopos_core.conformance.require_conformant()` checks that an installed
plugin's capability and readiness claims agree before a host offers it.
Registration also verifies that every operation accepts the protocol's initial
and resumed call shapes; runtime protocol checks alone only prove that method
names exist.

### Catalogue scope

**This table is the CATALOGUE, not what core drives.** Every row with a `yes`
under observe or settle is served by a separate rail package; core alone can
build the request and nothing else. The heading said "Built-in scope" until
2026-08-31, which described the package as it was before the 2.0 split.

| concrete network and asset | request | observe | settle | served by |
|---|---:|---:|---:|---|
| Bitcoin Testnet 4 / TBTC | yes | yes | 1 confirmation | `cryptopos-rail-bitcoin` |
| Ethereum Sepolia / ETH | yes | yes | 3 confirmations | `cryptopos-rail-evm` |
| Ethereum Sepolia / USDC | yes | yes | 3 confirmations | `cryptopos-rail-evm` |
| Polygon Amoy / POL | yes | yes | finalized block | `cryptopos-rail-evm` |
| Polygon Amoy / USDC | yes | yes | finalized block | `cryptopos-rail-evm` |
| Ootle Esmeralda / XTR | account address (no registered URI) | yes | committed/final | `cryptopos-rail-ootle` |
| Solana devnet / SOL | yes | no | no | **core, request-only** — URI carries a sale reference but no cluster |
| Solana devnet / USDC | yes | no | no | **core, request-only** — devnet mint and reference are carried |
| Minotari Esmeralda / XTM | yes | no | no | **core, request-only** — observation needs wallet/base-node gRPC |
| Dash testnet / TDASH | yes | no | no | **core, request-only** — Insight cannot prove ChainLocks |
| Zcash testnet / TAZEC | yes | no | no | **core, request-only** — no keyless address provider configured |
| Monero stagenet / XMR | no | no | no | **core** — held back until stagenet validation and a view-only sidecar |

The six request-only rows are what `register_builtins()` returns. The six
drivable rows are entry points, and **none of them is present until its package
is installed**: verified by enumerating `cryptopos.rails` against an environment
holding all four distributions.

Per-rail boundaries — genesis-hash pinning, chain-ID verification, reorg
exposure, and Ootle's two bindings — are in each rail package's own README,
where they can be revised with the code they describe.

This table is deliberately asymmetric. Breadth belongs in the registry;
chargeability belongs in runtime readiness. Adding an asset never makes it
settleable by implication.

Bitcoin Testnet 4 is the network `cryptopos-rail-bitcoin` drives because [BIP 95's proposed
Testnet 5](https://bips.dev/95/) is still a draft and does not yet define a
genesis block. Bitcoin
test-network addresses share an address format, and BIP-21 does not name the
network, so the payer wallet must still be configured for Testnet 4 even though
the observer independently verifies the [BIP 94 Testnet 4 genesis
hash](https://bips.dev/94/).

BIP 95 also documents the persistent short reorgs that motivated Testnet 5.
The built-in one-confirmation gate is therefore a test-flow gate, not a model
for accepting valuable Bitcoin payments. A host that treats test coins as
valuable should require a deeper operator policy.

## What it does

```python
from cryptopos_core import rails, rates, qr

# A rate is a number, a source, and a claim about how good the number is.
microcents, source, ok = rates.quote("btc", "mainnet")
#   -> (64001234000, "coinbase+kraken+bitstamp", True)  # $64,001.234
# `ok` is False when this is a fallback rather than a quote -- it is never
# dressed up as a feed answer. On mainnet a fallback is refused outright.

# The amount to invoice. Guaranteed statable exactly in a payment URI.
units = rails.invoice_amount(
    rails.rail_for("btc"), usd_cents=1099, rate_microcents=microcents
)

# A payment URI as a module grid, not markup.
grid = qr.modules_for(f"bitcoin:bc1q...?amount={units / 10**8:.8f}")
#   -> {"size": 29, "quiet": 4, "rows": ["111111101101...", ...]}
```

`modules_for` returns the grid rather than an SVG on purpose. Hosts that
sanitise stored HTML strip exactly the attributes an SVG needs — `d` and
`fill` — leaving a well-formed and completely blank image. Send the bits and
draw them at the surface.

### Addresses — the last check before money moves

An address that is wrong is money sent somewhere nobody holds a key to, and
there is no step after that. So this is checksums, not pattern-matching:
`^bc1[a-z0-9]{39}$` accepts a single-character typo that bech32 rejects.

```python
from cryptopos_core import addresses

addresses.validate("btc", "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4", "mainnet")
#   -> ("ok", "")

# The expensive mistake: valid, scannable, and money-losing.
addresses.validate("btc", "tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx", "mainnet")
#   -> ("refused", "that is a valid testnet address and this sale is mainnet;
#                   paying it would send mainnet coin to a testnet key")
```

The verdict is three-valued — `ok`, `refused`, `unchecked` — and `unchecked`
is not a soft `ok`. Solana addresses carry no checksum and Tari's format is
unspecified, so claiming those were verified would make the verdict
meaningless. `build_uri` refuses `unchecked` on mainnet.

Bech32/Bech32m (BIP-173/350), Base58Check, EIP-55 and Monero's Keccak
checksum, all against published vectors, all stdlib. Network binding works
wherever the chain encodes one; EVM and Solana encode none, and the module
says so rather than implying a check it did not perform.

### Legacy rail metadata and amount math

Twelve original rail definitions remain as frozen pure data—decimals, settle
gate, historical maturity notes, and payment binding. New applications should
use `catalog.BUILTIN_RAILS` and runtime readiness for capability decisions;
the table remains the source for exact unit and URI math:

```python
from cryptopos_core import rails, uri

rail = rails.rail_for("eth")
rail["maturity"]        # "works" -- real testnet reads AND a real payer
rail["gate_text"]       # "EIP-658 status == 0x1 AND confs >= 3"

# Rounded ONCE at display precision, then scaled to native, so a small
# amount does not round to zero on an 18-decimal chain.
wei = rails.usd_cents_to_native(rail, usd_cents=625)   # 1785000000000000
rails.format_amount(rail, wei)                          # "0.001785"

address = "0x52908400098527886E0F7030069857D2E4169EE7"
uri.build_uri("eth", {"address": address}, wei, mode="testnet")
#   -> "ethereum:0x52908400098527886E0F7030069857D2E4169EE7@11155111?value=1785000000000000"
```

`maturity` is retained for compatibility with the original terminal. It is
not the plugin readiness API and must not be used by a new host to enable a
charge button.

**Two amount forms live in `build_uri`, and the split is not cosmetic.**
BIP-21, Solana Pay and ZIP-321 carry a decimal amount; ERC-681 and Tari's
RFC-0154 deeplink carry the integer native amount. Sending the wrong one is
not a rounding difference — it is off by 10¹⁸.

`mode` is the sale's charge-time mode, so the chain id, token contract and
network authority baked into the URI cannot change when an operator flips a
setting mid-sale.

The vocabulary is closed: `demo`, `testnet`, or `mainnet`. A typo is refused
before a feed is called or a URI is built; it can never turn a misspelled
mainnet sale into permissive demo behavior.

**Use `rails.invoice_amount` to price a sale.** A decimal-amount URI carries
the display form, which truncates — invoice a Solana sale through the
lower-level `rates.native_for` and the QR asks for 73266000 lamports against
an invoice of 73266666. The customer pays what they were shown and the sale
sits 666 short of itself forever. `invoice_amount` cannot produce such an
amount, and `build_uri` raises `AmountNotRepresentable` rather than emit one.

What is **not** in the table: which endpoint an operator configured, and
whether a rail is switched on. Those change per deployment and belong to
whatever is hosting this.

### Reading the policy tier

Reads need no account and cost nothing, which is the whole point: a merchant's
promise is checkable by the customer holding the card, from any machine.

```python
from cryptopos_rail_ootle.chain import OotleReader, ceilings_wording

reader = OotleReader(loyalty_component="component_abc...")

facts, reason = reader.promise()
if facts is None:
    print(f"policy layer unavailable: {reason}")
else:
    for heading, body in ceilings_wording(facts):
        print(heading, "--", body)
```

**Every read is total. Nothing in `chain` raises.** A failed read returns
`(None, reason)`, because the rule above that module is absolute: *a sale must
never fail because the policy layer is down.* Check the sentinel; there is
nothing to catch.

## Why microcents

Cents × 10⁴, i.e. USD × 10⁶. Integer cents build the error into the unit
before any feed disagrees about anything: an asset quoted at $0.07745 is 7.745
cents, which in integer cents is 8 — a 3.3% error on a cheap asset, which is
exactly where a terminal handling more of them must be more precise, not less.

## What raises, and what doesn't

`chain` never raises — a failed policy read returns `(None, reason)`. Pricing
and URI building do raise, and everything they raise subclasses
`CryptoPosError`, so one `except` catches the lot:

| Exception | When |
|---|---|
| `RateUnavailable` | No usable price could be established |
| `FeedsDisagree` | Feeds answered and disagreed beyond tolerance (**subclasses `RateUnavailable`**) |
| `InvalidRate` | A conversion was handed a non-positive or non-integer rate |
| `InvalidAmount` | A money boundary was handed a non-positive or lossy amount |
| `InvalidAsset` | A quote request did not name a safe ASCII ticker |
| `InvalidMode` | A mode was not exactly `demo`, `testnet`, or `mainnet` |
| `InvalidPaymentIdentity` | A sale-binding reference is absent or malformed |
| `UnsupportedRail` | A rail is unknown or has no standardized payment URI |
| `AddressRefused` | The receiving address failed its check, or is uncheckable on mainnet |
| `AmountNotRepresentable` | The URI would have to truncate the invoiced amount |

They refuse to return a sentinel because there is no honest one — a caller
handed `None` will either display it, multiply by it, or encode it into a QR,
and all three are worse than stopping. Catch these at your framework boundary
and translate them into whatever your users see; the core does not know what a
screen is.

`FeedsDisagree` subclassing `RateUnavailable` is deliberate: a host that
already catches the general error keeps refusing correctly without being
changed. The safe behaviour is the one you get by doing nothing.

## What is deliberately not here

Persistence, scheduling, permissions, and the sale's state machine. Those are
where a host framework is genuinely better than a library, and a POS that hid
them inside a package would be fighting whatever it was embedded in.

Nor is anything that reads deployment state: endpoint ladders and operator
overrides, whether a rail is switched on, and the measured finality and
watchability tables that decide whether a rail can be charged in a given mode.
A library cannot confirm any of that without the host it came from.

If you want those too, the reference host is the Frappe/ERPNext app this
package was extracted from, which adds a Desk terminal, the `Crypto Sale`
state machine, a chain watcher, and optional ERPNext Sales Invoice booking.

## Real money is held to stricter rules

`quote(asset, "mainnet")` refuses to price from the demo constant, from a
single uncorroborated feed, or when feeds disagree by more than 2%. All three
raise `RateUnavailable` (or `FeedsDisagree`, which subclasses it), so a host
already catching that keeps refusing correctly without being changed.

`OotleReader` refuses a non-https indexer and refuses redirects that leave
HTTPS. Its `promise()` facts carry the indexer that answered — the default
is a **testnet** indexer, because no mainnet policy tier is published.

## Security model

Receiving addresses are checksum- and network-verified wherever their format
makes that possible. An address is never claimed as checked when a rail carries
no checksum or has no implemented format, and an unchecked address is refused
on mainnet. URI amounts must state the invoice exactly; mainnet prices require
at least two agreeing HTTPS feeds; network responses are size-bounded; and an
HTTPS read may redirect only to another HTTPS URL.

These checks verify form, network, amount, and transport. They do **not** prove
that the merchant controls a receiving key, that an external rate vendor is
honest, or that a host deployment has working chain watchers. The complete
guarantee and trust-boundary inventory ships in the source distribution as
`SECURITY.md`.

## Running the tests

Standard library only, no network, no framework, no test runner to install:

```bash
PYTHONPATH=src python -m unittest discover -s tests -t .
```

Every chain read is stubbed. A suite that needed a live indexer could not
assert the thing that matters most here — what happens when the indexer is
gone — so there is no network in it and there must never be one.

444 tests, no network, well under a second. Two installed-distribution checks
skip from a source-only run and execute against the built wheel.

Two of the tests are guards rather than tests of behaviour: one parses every
module and fails if anything outside the standard library is imported, the
other fails if the strings `frappe`, `erpnext` or `bench` appear anywhere in
the package. Both claims on this README decay silently otherwise.

## Licence

MIT. Vendors `qrcodegen.py` from Project Nayuki (MIT), unchanged and with its
notice intact — so the symbol a customer scans is produced by the same encoder
across every surface. A QR that differs between two surfaces of the same
terminal is a defect that only shows up at the counter.
