# cryptopos-core

Take blockchain payments in Python without adopting a framework, a wallet, or a
custodian. A *rail* is one asset on one named network; it builds the payer's
instruction, watches for the money, and returns a settlement decision. Your
application keeps the database, the scheduler, and the sale.

```bash
pip install git+https://github.com/dowoop/cryptopos-core
pip install git+https://github.com/dowoop/cryptopos-rail-bitcoin
```

**Not on PyPI yet**, so `pip install cryptopos-core` does not resolve — the repository is the
distribution until the name is registered. Install core first: a rail declares `cryptopos-core` as a
dependency and there is no index for pip to satisfy it from.

Zero dependencies, standard library only. Works in a web app, an ERP, a till, a
bot, or a script with nothing under it.

```python
from cryptopos_core.registry import RailRegistry

registry = RailRegistry()
registry.discover()                 # every plugin in the cryptopos.rails entry-point group
```

**New here? Read [The five calls](#the-five-calls), run
[Recipe 1](#1-take-one-payment-start-to-finish), then copy
[`examples/checkout_server.py`](examples/checkout_server.py).** That is a
complete checkout — form, QR, polling, settlement — in one stdlib file you can
run right now with no chain, no funds, and no configuration:

```bash
git clone https://github.com/dowoop/cryptopos-core && cd cryptopos-core
CRYPTOPOS_INIT=1 python3 examples/checkout_server.py     # http://127.0.0.1:8099
```

`examples/` ships in the repository and the sdist, **not in the wheel**, so an
install alone does not give you that file — clone for the server. Every
*recipe* below needs only the install: they use `cryptopos_core.testing`, which
is in the wheel.

## Status — read this before you take money with it

**Not audited.** This library has been reviewed by its author and by adversarial
AI review. No external security audit has been performed, and it has never
handled mainnet funds.

**What has been proven, and how.** Every drivable rail has settled real testnet
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

Every Python example in this file is executed by `tools/readme.py`, which fails
if a `# ->` claim is not exactly what the code produces — so the recipes below
are checked rather than remembered. `--wheel` re-runs them in a fresh isolated
interpreter against `dist/*.whl`, and refuses if that wheel does not match this
tree. Blocks that need a live chain or real funds are marked and shown, not
run.

---

## The five calls

Whatever your framework, the integration is the same five calls in the same
order. Everything else in your host is storage and scheduling.

```
validate_recipient(addr)              is this address safe to be paid at?
        │
capture_baseline(addr, config)        where does the chain stand right now?
        │                             ── the payer must not see anything yet ──
create_request(intent)                the URI/QR the customer pays
        │
observe(intent, config, previous)     one bounded read; loop until .complete
        │
settle(intent, batch, claimed_ids)    pending | settled | needs-review
```

`readiness(config)` sits outside the sale: ask it once at start-up to find out
which rails this deployment can actually charge.

**The five things a host must get wrong-proof.** Each of these has cost this
project real money at least once:

1. **Two open sales must not share a receiving address.** On any rail whose
   `binding_category` is `not-unconditional` — which is most of them — the
   first sale to poll credits every unclaimed transfer it can see, including
   the other sale's. No race is needed. Reproduced against the example in this
   repository: a sale invoiced 100 settled on 350 and left the customer who
   paid 250 unpaid. And "share" includes *one after the other* — an address
   that was shown to a payer can still be paid, so a finished sale's QR settles
   whatever sale holds that address next. Derive an address per sale, allocate
   each one once, and never reissue it.
2. **Claim `decision.transaction_ids` exclusively — per recipient — in the
   same write as the settled state.** `settle` is pure: it credits whatever you
   did not tell it was already spent. Reading the claimed set, settling, then
   writing is the obvious shape and it is wrong — two workers read the same set
   before either writes, and one transfer settles two invoices. What saves you
   is that claiming can *fail*: a `PRIMARY KEY` over the claim, and the losing
   `INSERT` rolling back the sale state with it.
   **Key it on `(rail, recipient, transaction_id)`, not the id alone.** A transaction
   id is not an exclusive payment identifier: one chain transaction can carry
   outputs to several addresses, so an exchange batching its withdrawals pays
   two of your sales at once. Claiming the bare id settles the first and leaves
   the second — whose customer really paid — looking unpaid. Reproduced against
   the example here. Scoping by recipient is exact precisely *because* of
   obligation 1: two sales never share an address, so they never share a
   (recipient, transaction) pair, while the same output still cannot be
   credited twice to the sale that owns that address. The rail belongs in the
   key too: transaction ids and address strings are per-network, and two rails
   can mint the same-looking pair.
3. **Capture the baseline before the payer sees the request.** It pins the
   chain position the sale starts from. Capture it late and a transfer that
   predates the sale can be credited to it.
4. **Loop `observe` until the batch reports `.complete`.** It returns what one
   provider call could read. Deciding on a partial read is deciding on a
   partial payment.
5. **`needs-review` is a real outcome, not an error, and it needs somewhere to
   go.** The rails here return it for money that is *established* and not
   creditable — a matured transfer that arrived after the window, say. A
   transaction whose status could not be established stays `pending` instead,
   because not knowing is the absence of a decision and a terminal state taken
   on it loses a sale to a transient read; routing persistent uncertainty to a
   person is your grace policy, not the rail's. None of that is enforced by
   `SettlementDecision`, so read the reason rather than assuming an amount. And
   a status string is not a queue: give a person a list they actually see.

---

# Cookbook

## 1. Take one payment, start to finish

This runs with no chain and no funds: `MemoryRail` is a scripted rail that
ships **in the package**, so it works from a plain `pip install` (recipe 8
builds one). Swap it for `registry.discover()` and a real rail key and nothing
else changes.

<!-- readme: new -->
```python
from cryptopos_core.conformance import require_conformant
from cryptopos_core.plugin import PaymentIntent
from cryptopos_core.registry import RailRegistry
from cryptopos_core.testing import MemoryRail

registry = RailRegistry()
registry.register(MemoryRail())
rail = registry.get("memory:testnet/native:tok")

chain = {"endpoint": "memory://", "tip": 60, "page": 20, "transfers": []}
require_conformant(rail, chain)          # capability claims must match readiness
rail.readiness(chain).chargeable         # -> True
```

Check the address before anything else. This is the last moment at which a
mistake is still free:

```python
rail.validate_recipient("mem1alice")     # -> ('ok', '')
```

Now capture the baseline, **then** build the request. The order matters (point 3
above):

```python
baseline = rail.capture_baseline("mem1alice", chain)
intent = PaymentIntent(
    intent_id="sale-1042",
    rail_key=rail.key,
    recipient="mem1alice",
    amount_native=250,                   # smallest unit; never a float
    created_at_epoch=1_787_100_000,
    expires_at_epoch=1_787_101_800,
    baseline=baseline,
)
rail.create_request(intent).uri          # -> 'memory:mem1alice?amount=250'
```

Show that URI to the customer. Time passes; the chain moves; the money arrives:

```python
chain["tip"] = 100
chain["transfers"] = [
    {"id": "tx-a", "to": "mem1alice", "amount": 250, "confs": 3,
     "height": 71, "at": 1_787_101_000},        # inside the payment window
]
```

Poll. `observe` is bounded, so loop until it says it has caught up with the
provider's tip (point 4):

```python
batch = rail.observe(intent, chain)
while not batch.complete:
    batch = rail.observe(intent, chain, batch)

batch.observed_through_tip               # -> 100
len(batch.transfers)                     # -> 1
```

Then decide. `claimed_transaction_ids` is every transaction id your database has
already credited **at this sale's recipient** — not across all sales (point 2):

```python
decision = rail.settle(intent, batch, claimed_transaction_ids=frozenset())
decision.state                           # -> 'settled'
decision.credited_native                 # -> 250
decision.transaction_ids                 # -> ('tx-a',)
```

Store `decision.transaction_ids` in the same database transaction that writes
the state. Here is what that guard buys you — the same transfer, offered to a
second sale, is refused:

```python
replay = rail.settle(intent, batch, claimed_transaction_ids=frozenset({"tx-a"}))
replay.state                             # -> 'pending'
```

Without it, one transfer settles two invoices.

**And the scope of that set matters as much as its contents.** One chain
transaction can pay several addresses at once — an exchange batching its
withdrawals does exactly that — so a claim recorded against the bare
transaction id would refuse the *other* sale it legitimately paid:

```python
alice, bob = "mem1alice", "mem1bob"
batched = {**chain, "tip": 140, "transfers": [
    {"id": "batch-1", "to": alice, "amount": 250, "confs": 3, "height": 120,
     "at": 1_787_101_000},
    {"id": "batch-1", "to": bob, "amount": 400, "confs": 3, "height": 120,
     "at": 1_787_101_000},
]}

def sale_at(name, recipient, amount):
    opened = rail.capture_baseline(recipient, {**chain, "tip": 100})
    return PaymentIntent(name, rail.key, recipient, amount,
                         1_787_100_000, 1_787_101_800, baseline=opened)

for_alice, for_bob = sale_at("sale-x", alice, 250), sale_at("sale-y", bob, 400)
first = rail.settle(for_alice, rail.observe(for_alice, batched), frozenset())
first.state                              # -> 'settled'
```

Alice's sale has now credited `batch-1`. Pass that id to Bob's settlement as if
it were globally spent and Bob — who was paid, in the same transaction — goes
unpaid:

```python
rail.settle(for_bob, rail.observe(for_bob, batched), frozenset({"batch-1"})).state
#   -> 'pending'
```

Scope the set to the recipient and both settle, because two sales never share
an address:

```python
rail.settle(for_bob, rail.observe(for_bob, batched), frozenset()).state
#   -> 'settled'
```

## 2. Put it behind a web page

[`examples/checkout_server.py`](examples/checkout_server.py) is a complete
checkout in one file: an amount form, a QR page, a JSON status endpoint the page
polls, and a background watcher. It is `http.server` and nothing else, because
the only framework-shaped code in a crypto checkout is "read a request, write a
response" — the rest is the five calls.

```bash
CRYPTOPOS_INIT=1 python3 examples/checkout_server.py
# rail memory:testnet/native:tok -- a derived address per sale
# demo rail: a scripted payer settles each sale about 8s after you charge it
# http://127.0.0.1:8099        (review queue: /review)
```

`CRYPTOPOS_INIT=1` is needed once, to create the file that remembers which
derivation indices have been handed out — and how many have gone unpaid, so the
server can **refuse new sales** before it walks the watching wallet past its gap
limit. That refusal is the backpressure the gap-limit warning asks for; a
warning in a README stops nobody from posting a hundred abandoned checkouts. After that its absence is a symptom
rather than a fresh start, and the server refuses rather than reissuing
addresses that may already be live.

Point it at a real chain with environment variables and no code change:

```bash
pip install git+https://github.com/dowoop/cryptopos-rail-bitcoin
CRYPTOPOS_RAIL=bitcoin:testnet4/native:btc \
CRYPTOPOS_ENDPOINT=https://mempool.space/testnet4/api \
CRYPTOPOS_XPUB=tpub... python3 examples/checkout_server.py
```

`CRYPTOPOS_XPUB` is the **account** extended public key. Give it one and every
sale gets its own derived address, allocated once and never reissued.

Give it `CRYPTOPOS_RECIPIENT` instead — a single fixed address — and the server
accepts exactly **one sale, ever**, then refuses. Not one at a time: one. A
payment instruction cannot be withdrawn, so the QR from a finished sale is still
payable, and the next sale at that address would settle on the previous
customer's money. "One at a time" sounds like the safe version of a shared
address and is not; there isn't one.

(`examples/` is in the repository and the sdist, not the wheel. `pip install`
gives you `cryptopos_core.testing`, which every recipe here uses; clone the
repository for the server.)

**Four things the example cannot do for you, stated rather than implied.** The
`PaymentRequest` fields beside a URI are metadata, not proof of it, so the
example parses the URI and checks the whole payment identity: the scheme, the
chain id, the token contract for an ERC-20 rail, and the address the money
actually reaches. A destination check alone was not enough — a URI paying an
attacker can mention the merchant in a note, and `@1` on a Sepolia sale sends
the customer to mainnet, where the same address exists. The amount is
checked against the invoice too — in the decimal form BIP-21 uses and the
integer form ERC-681 uses, which differ by a factor of 10^decimals — so nothing
about the instruction is left on trust. Query parameters are whitelisted rather
than ignored, because some of them are not decoration.
BIP-72's `r` tells a capable wallet to disregard the address entirely and fetch
a payment request from a URL, which may name any output and need not be signed;
ERC-681's `gasPrice` and `gasLimit` are fee *instructions*, and a one-wei
invoice carrying them can cost the payer twenty-one ETH in fees while the sale
settles perfectly. Both are refused — a wallet can estimate its own fee. A network whose scheme
the example has no parser for is refused rather than shown to a payer. A
BIP-32 key does not carry its own path, so nothing can tell an account key from
another branch at the same depth — refusing a master key removes the only case
that is provable, and the rest is yours to get right. A restored *older* state
file is valid JSON and rolls the allocator backwards: the file carries a
fingerprint of the key, rail and mode it belongs to, so a rotated key or a
different chain cannot inherit its counters, but nothing distinguishes an old
copy of its own from the current one. And a payment that lands after a sale expired
is never misattributed — the address is not reused — but nothing here goes back
to look for it, so late money needs wallet reconciliation you build separately.

The three pieces worth lifting into your own app, whatever it is written in:

**Build the registry once, at start-up, not per request.** `discover()` reads
entry points and validates every plugin; that is start-up work.

```python
def load_rail(key, config):
    registry = RailRegistry()
    registry.discover()
    rail = registry.get(key)             # raises RailNotInstalled if absent
    require_conformant(rail, config)
    readiness = rail.readiness(config)
    if not readiness.chargeable:
        raise SystemExit(f"{key} cannot be charged here: {readiness.unavailable}")
    return rail
```

**Let the database refuse a transaction that is already spent.** The claimed
set is not a cache you read before deciding — it is a uniqueness constraint that
makes the second claim fail:

<!-- readme: skip -->
```sql
CREATE TABLE credited_tx (
    rail_key  TEXT NOT NULL,                          -- ids are not global
    recipient TEXT NOT NULL,                          -- the sale's own address
    tx_id     TEXT NOT NULL,
    sale_id   TEXT NOT NULL REFERENCES sale(id),      -- no orphan claims
    PRIMARY KEY (rail_key, recipient, tx_id)          -- NOT tx_id alone
);
```

<!-- readme: skip -->
```python
try:
    with db.transaction():
        # The transition is CONDITIONAL. A worker arriving late holds a
        # decision from a stale snapshot -- typically `needs-review`, which
        # carries NO transaction ids, so the PRIMARY KEY above cannot catch
        # it. Only `AND state = 'pending'` can.
        rows = db.execute(
            "UPDATE sale SET state=?, credited=? "
            "WHERE id=? AND state='pending' AND lease=?",
            (decision.state, decision.credited_native, sale_id, token)).rowcount
        if rows != 1:
            # Either another worker decided this sale, or this worker's lease
            # expired and someone else owns it now. Either way: claim nothing.
            raise AlreadyDecided(sale_id)
        db.executemany(
            "INSERT INTO credited_tx (rail_key, recipient, tx_id, sale_id) "
            "VALUES (?, ?, ?, ?)",
            [(rail.key, recipient, t, sale_id) for t in decision.transaction_ids])
except UniqueViolation:                        # NOT bare IntegrityError
    pass          # another sale claimed it first: stay pending, poll again
except AlreadyDecided:
    pass          # this sale already has an answer; the stored one is the truth
```

Four details, each of which has a failure behind it. The **composite key** is
why a batched payout does not strand the second sale it paid. The **rollback** is why a
sale is never marked paid on someone else's money. The **row count** is why a
stale worker cannot reopen a settled sale — the uniqueness constraint is blind
to a decision that claims nothing. And catching **the uniqueness violation
specifically**, rather than every `IntegrityError`, is why an unrelated
constraint failure is not silently misread as a lost claim race.

**One worker owns a sale from its observation to its write.** A conditional
update is not enough on its own: two workers reading the chain a block apart
reach different conclusions, and then the *first* writer wins rather than the
better-informed one — a `needs-review` computed from a stale read beats a
`settled` computed from a fresh one, and the paid sale never settles. Take a
lease, carry its token into the write, and let the write refuse anyone who does
not hold it:

<!-- readme: skip -->
```python
for sale in open_sales():
    token = lease(sale.id, ttl=30)      # returns None if someone else holds it
    if token is None:
        continue
    try:
        poll_once(sale, token)          # observe AND write under this token
    except RailProviderError as exc:
        log.warning("watch %s: provider unavailable: %s", sale.id, exc)
    # An InvalidRailPlugin, an integrity violation, or a bug in your own code
    # repeats deterministically. Swallowing those retries them forever, and the
    # customer's money sits on-chain against a sale nobody is told about.
    # Let them raise, fail the job, and page someone.
    finally:
        release(sale.id, token)         # only if the token still matches
```

The token is what makes it a lease rather than a hint. A worker whose provider
read outlives its lease must not release the lease its successor now holds, and
must not write through it — so `UPDATE ... WHERE id = ? AND state = 'pending'`
gains `AND lease = ?`, and a release is `WHERE id = ? AND lease = ?`. Without
that, a slow worker's stale conclusion still lands.

**Let a provider error stay an error — and only a provider error.** A failed
read is not a verdict; leave the sale pending and try again. Catching
everything is worse than catching nothing, which is why the block above names
one exception rather than `Exception`.

FastAPI, Flask and Django change only the outermost layer: a route that calls
`start_sale`, a route that returns the state as JSON, and a job in your existing
queue instead of the example's thread.

## 3. Draw the QR

`modules_for` returns a grid of bits, not markup — deliberately. Hosts that
sanitise stored HTML strip exactly the attributes an SVG needs (`d`, `fill`),
leaving a well-formed and completely blank image. Send the bits; draw them at
the surface.

```python
from cryptopos_core.qr import modules_for

grid = modules_for("bitcoin:tb1qexample?amount=0.00125")
grid["size"]                             # -> 29
grid["quiet"]                            # -> 4
len(grid["rows"])                        # -> 29
```

Each row is a string of `"0"`/`"1"`. The quiet zone is *not* included in the
rows — add `quiet` modules of margin yourself, on all four sides. Scanners fail
intermittently without it, and intermittently is the worst way for a payment
surface to fail: it looks like the customer's phone.

```python
def qr_svg(uri, scale=8):
    grid = modules_for(uri)
    quiet, side = grid["quiet"], grid["size"] + grid["quiet"] * 2
    squares = "".join(
        f'<rect x="{x + quiet}" y="{y + quiet}" width="1" height="1"/>'
        for y, row in enumerate(grid["rows"])
        for x, module in enumerate(row) if module == "1"
    )
    return (f'<svg viewBox="0 0 {side} {side}" width="{side * scale}" '
            f'height="{side * scale}" shape-rendering="crispEdges">'
            f'<rect width="{side}" height="{side}" fill="#fff"/>'
            f'<g fill="#000">{squares}</g></svg>')

qr_svg("bitcoin:tb1qexample?amount=0.00125").startswith("<svg")   # -> True
```

`shape-rendering="crispEdges"` is not decoration: without it a browser
antialiases module edges and a phone camera can lose the symbol.

## 4. Price the sale in the customer's money

Your customer thinks in dollars; the chain thinks in integers. Two steps, and
the second one is the one that has a trap in it.

Quoting needs the network, so this block is shown rather than run:

<!-- readme: skip -->
```python
from cryptopos_core import rates

microcents, source, ok = rates.quote("btc", "mainnet")
#   e.g. (64001234000, "coinbase+kraken+bitstamp", True)   # $64,001.234
# The number depends on what the feeds say when you ask, so it is an
# illustration rather than a claim. The arrow notation used elsewhere in this
# file is reserved for values the gate actually evaluates.
```

`ok` is `False` when the number is a fallback rather than a quote — it is never
dressed up as a feed answer, and on mainnet a fallback is refused outright.
Microcents are cents × 10⁴; [why](#why-microcents) is worth two minutes.

Now convert. **Use `rails.invoice_amount`, not the lower-level helpers:**

```python
from cryptopos_core import rails

btc = rails.rail_for("btc")
rails.invoice_amount(btc, usd_cents=1099, rate_microcents=64001234000)   # -> 17171
```

The trap: a decimal-amount URI (BIP-21, Solana Pay, ZIP-321) carries the
*display* form, which truncates. Price a Solana sale through the lower-level
`rates.native_for` and the QR can ask for 73,266,000 lamports against an invoice
of 73,266,666. The customer pays exactly what they were shown and the sale sits
666 short of itself forever. `invoice_amount` cannot produce such an amount, and
`build_uri` raises `AmountNotRepresentable` rather than emit one.

Rounding once at display precision also stops a small amount vanishing on an
18-decimal chain:

```python
eth = rails.rail_for("eth")
wei = rails.usd_cents_to_native(eth, usd_cents=625)
wei                                      # -> 1785000000000000
rails.format_amount(eth, wei)            # -> '0.001785'
```

## 5. Refuse a bad receiving address

An address that is wrong is money sent somewhere nobody holds a key to, and
there is no step after that. So this is checksums, not pattern-matching:
`^bc1[a-z0-9]{39}$` accepts a single-character typo that bech32 rejects.

```python
from cryptopos_core import addresses

addresses.validate("btc", "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4", "mainnet")
#   -> ('ok', '')
```

The expensive mistake — valid, scannable, and money-losing:

```python
verdict, why = addresses.validate(
    "btc", "tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx", "mainnet")
verdict                                  # -> 'refused'
```

> that is a valid testnet address and this sale is mainnet; paying it would send
> mainnet coin to a testnet key

The verdict is three-valued, and `unchecked` is **not** a soft `ok`:

```python
addresses.validate("sol", "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM", "mainnet")[0]
#   -> 'unchecked'
```

Solana addresses carry no checksum and Tari's format is unspecified, so claiming
those were verified would make the verdict meaningless. `build_uri` refuses
`unchecked` on mainnet. Bech32/Bech32m (BIP-173/350), Base58Check, EIP-55 and
Monero's Keccak checksum, all against published vectors, all stdlib.

Building the URI yourself, when you are not going through a rail:

```python
from cryptopos_core import uri

uri.build_uri("eth", {"address": "0x52908400098527886E0F7030069857D2E4169EE7"},
              1785000000000000, mode="testnet")
#   -> 'ethereum:0x52908400098527886E0F7030069857D2E4169EE7@11155111?value=1785000000000000'
```

`mode` is the sale's charge-time mode, so the chain id, token contract and
network authority baked into the URI cannot change when an operator flips a
setting mid-sale. The vocabulary is closed — `demo`, `testnet`, `mainnet` — and
a typo is refused before a feed is called, so a misspelled mainnet sale can
never become permissive demo behaviour.

## 6. Offer only the rails you can actually charge today

Installed is not the same as usable. A rail whose endpoint is unreachable, or
whose provider does not serve the method it needs, must not appear as a payment
option. Ask readiness, once, at start-up:

```python
def payment_options(registry, configuration_for):
    """Rail keys a customer may actually be shown, with reasons for the rest."""
    offered, withheld = [], {}
    for key in registry.keys():
        rail = registry.get(key)
        readiness = rail.readiness(configuration_for(key))
        if readiness.chargeable:
            offered.append(key)
        else:
            withheld[key] = readiness.unavailable
    return offered, withheld

offered, withheld = payment_options(registry, lambda key: chain)
offered                                  # -> ['memory:testnet/native:tok']
```

A rail that is not ready tells you why, per capability, rather than disappearing:

```python
unset = {"endpoint": "", "tip": 60, "page": 20}          # everything but the endpoint
offered, withheld = payment_options(registry, lambda key: unset)
offered                                  # -> []
dict(withheld["memory:testnet/native:tok"])["observation"]
#   -> 'no endpoint configured'
```

Per capability means *per capability*: an unreachable provider stops this rail
observing, and does not stop it checking an address, building a request, or
deciding a settlement, because none of those read anything.

```python
sorted(registry.get("memory:testnet/native:tok").readiness(unset).ready)
#   -> ['address-validation', 'payment-request', 'settlement']
```

Settlement is in that list because `settle` is a pure function of an intent and
a batch you already hold — it reads nothing. Only observation needs the
provider, and `chargeable` is still `False`, because it needs all four.

Readiness reports what this deployment can actually do, not what the rail can
do in principle. A configuration that would leave the observation loop unable
to advance is refused here rather than discovered halfway through a sale.

`chargeable` means all four capabilities passed *this deployment's* checks. It
does **not** mean the payment is bound to the sale — see
[Capability](#capability-and-what-chargeable-does-not-mean).

## 7. Handle all three settlement outcomes

`settle` returns one of exactly three states. A host that treats it as a boolean
has a bug waiting for a bad day.

```python
def apply(decision, sale_id):
    if decision.state == "settled":
        return f"{sale_id}: paid {decision.credited_native}, ids {decision.transaction_ids}"
    if decision.state == "pending":
        return f"{sale_id}: keep watching — {decision.reason}"
    return f"{sale_id}: A PERSON MUST LOOK — {decision.reason}"

apply(decision, "sale-1042")
#   -> "sale-1042: paid 250, ids ('tx-a',)"
```

**`pending`** — nothing conclusive yet, which is not the same as nothing
arrived. It covers a confirmed part payment, money still maturing toward a
rail's confirmation depth, and a read that could not establish a transaction's
status. Keep polling; expiry is yours, because the rail does not know your
refund policy.

**And expiry is a cutoff on the payer, not on the chain.** A transfer made
honestly at minute fourteen of a fifteen-minute window still needs its
confirmations — three on Sepolia, a twenty-minute median block on Bitcoin
testnet4 — so it matures *after* the sale's clock runs out. Closing the sale at
the deadline throws away a payment that was about to succeed. Let money that
arrived in time go on maturing for a grace period, and only then ask a person.
The example does exactly that.

**`settled`** — credited money and at least one transaction id, guaranteed
together. A settled decision can never carry zero ids; the dataclass refuses to
be constructed that way.

**`needs-review`** — the rail saw money it will not credit and will not guess
about. This is the state that protects you. Here a transfer arrived after the
sale's window had closed: it is real money, at the right address, and not this
sale's to take:

```python
late = {**chain, "transfers": [
    {"id": "tx-late", "to": "mem1alice", "amount": 250, "confs": 3,
     "height": 71, "at": intent.expires_at_epoch + 1},
]}
verdict = rail.settle(intent, rail.observe(intent, late))
verdict.state                            # -> 'needs-review'
verdict.credited_native                  # -> 0
verdict.sighted_native                   # -> 250
```

Compare that with a read that simply failed. "I could not find out" is the
*absence* of a decision, so it stays retryable rather than becoming a case for
a person on the first hiccup:

```python
doubted = {**chain, "transfers": [
    {"id": "tx-?", "to": "mem1alice", "amount": 250, "confs": 3, "height": 71,
     "unreadable": True},                    # the provider was asked; it did not answer
]}
rail.settle(intent, rail.observe(intent, doubted)).state
#   -> 'pending'
```

`sighted_native` is 250 while `credited_native` is 0: the rail reports what it
saw *and* what it was willing to credit, and the gap is exactly what a human
has to look at. **Never resolve `needs-review` automatically**, and give it a
real destination — a status string nobody queries is not a queue.

That is the distinction worth holding on to: *"the chain says this money is not
yours"* is a decision, and *"I could not find out"* is the absence of one. A
terminal state taken on the second is a sale lost to a transient read.

## 8. Write your own rail

A rail is a plain object with four attributes and six methods. No base class, no
registration decorator, no import-time side effects. Here are the declarations
of [`cryptopos_core.testing.MemoryRail`](src/cryptopos_core/testing.py) with
the bodies elided — read that file for the working implementation, which is the
scripted rail every recipe above runs on:

<!-- readme: skip -->
```python
class MemoryRail:
    key = "memory:testnet/native:tok"            # must equal f"{network.key}/{asset.key}"
    network = Network("memory", "testnet", True)
    asset = Asset("native", "tok", "TOK", 2)
    capabilities = frozenset({ADDRESS_VALIDATION, PAYMENT_REQUEST, OBSERVATION, SETTLEMENT})
    binding_category = NOT_UNCONDITIONAL

    def readiness(self, configuration): ...
    def validate_recipient(self, recipient): ...
    def capture_baseline(self, recipient, configuration): ...
    def create_request(self, intent): ...
    def observe(self, intent, configuration, previous=None): ...
    def settle(self, intent, observations, claimed_transaction_ids=frozenset()): ...
```

`register()` validates the shape before your rail ever sees a sale — the key
format, the identity types, the capability vocabulary, and that every method
accepts both the initial and the resumed call shape. Runtime protocol checks
alone only prove that method names exist:

<!-- readme: raises -->
```python
registry.register(MemoryRail())          # DuplicateRail - it is already registered
registry.get("no:such/rail:key")         # RailNotInstalled - the honest answer, not a stub
```

Three rules the protocol enforces and you should not fight:

- **Declare capabilities honestly.** `conformance_issues` fails a plugin whose
  `readiness` claims something `capabilities` did not declare, and a declared
  capability that is neither ready nor explained is also a violation.
- **Return immutable facts, never decisions about the host's business.** A rail
  says "this transfer, this amount, this many confirmations". Whether that is
  enough to ship the goods is the host's call.
- **Never add a required field to a published protocol type.** Both
  `RecipientBaseline.payment_component` and
  `ObservationBatch.unresolved_transaction_ids` are optional with defaults for
  this reason: a required field made every installed 0.1.0 wheel undriveable
  while the source suite stayed green, because the suite tests the source and
  the deployment runs the install.

Ship it as its own distribution with an entry point, and installing it is what
adds the rail — nothing in the host is edited:

<!-- readme: skip -->
```toml
[project.entry-points."cryptopos.rails"]
my-network-token = "my_rail:my_network_token"
```

## 9. Test your host with no chain, no funds, no network

This is what `cryptopos_core.testing.MemoryRail` is really for. Your host's bugs — the double-credit,
the partial read, the late baseline — are all reachable without a chain, and
they are the bugs that cost money.

```python
def scripted(*transfers, tip=100, page=1000):
    return {"endpoint": "memory://", "tip": tip, "page": page,
            "transfers": [dict(to="mem1alice", confs=3, at=1_787_101_000, **t)
                          for t in transfers]}
```

**A payment that arrives in two parts settles once it is whole:**

```python
half = scripted({"id": "tx-1", "amount": 100, "height": 71},
                {"id": "tx-2", "amount": 150, "height": 72}, tip=100)
first = rail.capture_baseline("mem1alice", {**chain, "tip": 60})
part = PaymentIntent("sale-2", rail.key, "mem1alice", 250, 1_787_100_000,
                     1_787_101_800, baseline=first)
rail.settle(part, rail.observe(part, half)).state        # -> 'settled'
```

**A bounded provider forces several reads, and the answer must not change:**

```python
paged = scripted({"id": "tx-1", "amount": 250, "height": 71}, tip=100, page=7)
batch = rail.observe(part, paged)
reads = 1
while not batch.complete:
    batch = rail.observe(part, paged, batch)
    reads += 1
reads > 1                                # -> True
rail.settle(part, batch).state           # -> 'settled'
```

**One transfer cannot pay two sales — if you claim it.** This is the scenario
that breaks hosts, so it is worth having as a test of your own:

```python
shared = scripted({"id": "tx-shared", "amount": 100, "height": 71}, tip=100)
opened = rail.capture_baseline("mem1alice", {**chain, "tip": 60})

def sale(name):
    return PaymentIntent(name, rail.key, "mem1alice", 100, 1_787_100_000,
                         1_787_101_800, baseline=opened)

credited = set()
sale_a = sale("sale-a")
first = rail.settle(sale_a, rail.observe(sale_a, shared), frozenset(credited))
first.state                              # -> 'settled'
credited.update(first.transaction_ids)

sale_b = sale("sale-b")
second = rail.settle(sale_b, rail.observe(sale_b, shared), frozenset(credited))
second.state                             # -> 'pending'
```

That second `pending` is entirely the host's doing. The rail credited honestly
both times; what stopped the double payment was `credited` having grown first.
So the property you must actually test is not this call — it is that **two
concurrent workers cannot both observe `credited` without it**. Reading it,
settling, and then updating it is a lost update, and a lost update here is a
customer's money paying someone else's invoice.

**A batch from another sale is refused rather than misapplied:**

<!-- readme: raises -->
```python
rail.settle(intent, batch)               # InvalidRailPlugin - these observations belong to sale-2
```

That last refusal is the protocol catching a host bug for you: `require_intent`
compares the rail, the intent id, the recipient, the provider and the baseline
tip, so observations can never be applied to the wrong sale.

---

# Reference

## Capability, and what `chargeable` does not mean

`chargeable=True` means a rail declares all four capabilities: it can validate a
recipient, build a payment request, observe the chain and return a settlement
decision. It does **not** mean the payment is bound to the sale. Read each rail
package's binding section; they differ enormously, and the weakest is the
default on four of the seven rails.

`binding_category` is the rail's own declaration. `unconditional-per-sale` means
the rail itself ties money to one sale before the host chooses any
receiving-address strategy; `not-unconditional` means it does not, and the host
must strengthen it (typically by deriving a fresh address per sale). Absence is
read pessimistically as `not-unconditional`, so plugins published before the
declaration existed stay driveable and understate their binding safely.

EVM readiness exercises the actual full-block or token-log method the rail
needs, not only `eth_chainId`. It still cannot prove that a provider is honest.
It is intentionally stricter than "a QR can be built".

## Installable rails, and what core drives

**Core drives no rail on any real network.** `register_builtins()` registers six
*request-only* catalogue entries — chains this package can describe and build a
payment request for, and cannot observe or settle. Every rail that moves real
money is an installed package discovered through the `cryptopos.rails`
entry-point group. Asking the registry for one without installing it raises
`RailNotInstalled`, which is the honest answer rather than a stub.

The one exception is deliberate and cannot reach a chain:
`cryptopos_core.testing.MemoryRail` is fully conformant — it registers, passes
`require_conformant`, observes and settles — because a test double that the
protocol would reject cannot test a host against the protocol. It names a
network that does not exist, publishes no entry point, and is not returned by
`register_builtins()`, so `discover()` will never find it and no deployment can
acquire it by accident. You have to import it on purpose.

```python
from cryptopos_core.registry import RailRegistry

catalogue = RailRegistry()
len(catalogue.register_builtins())       # -> 6
```

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
is installed** — verified by enumerating `cryptopos.rails` against an
environment holding all four distributions.

This table is deliberately asymmetric. Breadth belongs in the registry;
chargeability belongs in runtime readiness. Adding an asset never makes it
settleable by implication. Per-rail boundaries — genesis-hash pinning, chain-ID
verification, reorg exposure, and Ootle's two bindings — are in each rail
package's own README, where they can be revised with the code they describe.

Bitcoin Testnet 4 is the network `cryptopos-rail-bitcoin` drives because
[BIP 95's proposed Testnet 5](https://bips.dev/95/) is still a draft and does not
yet define a genesis block. Bitcoin test-network addresses share an address
format, and BIP-21 does not name the network, so the payer wallet must still be
configured for Testnet 4 even though the observer independently verifies the
[BIP 94 Testnet 4 genesis hash](https://bips.dev/94/). BIP 95 also documents the
persistent short reorgs that motivated Testnet 5, so that rail's
one-confirmation gate is a test-flow gate, not a model for accepting valuable
Bitcoin payments.

What is **not** in the table: which endpoint an operator configured, and whether
a rail is switched on. Those change per deployment and belong to whatever is
hosting this.

## Reading the policy tier

Optional, and it lives in `cryptopos-rail-ootle`. Reads need no account and cost
nothing, which is the whole point: a merchant's promise is checkable by the
customer holding the card, from any machine.

<!-- readme: skip -->
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

**Every read is total. Nothing in that module raises.** A failed read returns
`(None, reason)`, because the rule above it is absolute: *a sale must never fail
because the policy layer is down.* Check the sentinel; there is nothing to
catch.

## Why microcents

Cents × 10⁴, i.e. USD × 10⁶. Integer cents build the error into the unit before
any feed disagrees about anything: an asset quoted at $0.07745 is 7.745 cents,
which in integer cents is 8 — a 3.3% error on a cheap asset, which is exactly
where a terminal handling more of them must be more precise, not less.

## What raises, and what doesn't

Pricing and URI building raise; everything they raise subclasses
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
| `RailNotInstalled` | A rail key was asked for and no installed package provides it |
| `DuplicateRail` | Two plugins claim the same rail key |
| `InvalidRailPlugin` | A plugin, or a value handed to one, violates the protocol |

They refuse to return a sentinel because there is no honest one — a caller
handed `None` will either display it, multiply by it, or encode it into a QR,
and all three are worse than stopping. Catch these at your framework boundary
and translate them into whatever your users see; the core does not know what a
screen is.

`FeedsDisagree` subclassing `RateUnavailable` is deliberate: a host that already
catches the general error keeps refusing correctly without being changed. The
safe behaviour is the one you get by doing nothing.

## Real money is held to stricter rules

`quote(asset, "mainnet")` refuses to price from the demo constant, from a single
uncorroborated feed, or when feeds disagree by more than 2%. All three raise
`RateUnavailable` (or `FeedsDisagree`, which subclasses it), so a host already
catching that keeps refusing correctly without being changed.

`OotleReader` refuses a non-https indexer and refuses redirects that leave
HTTPS. Its `promise()` facts carry the indexer that answered — the default is a
**testnet** indexer, because no mainnet policy tier is published.

## What is deliberately not here

Persistence, scheduling, permissions, and the sale's state machine. Those are
where a host framework is genuinely better than a library, and a POS that hid
them inside a package would be fighting whatever it was embedded in.

Nor is anything that reads deployment state: endpoint ladders and operator
overrides, whether a rail is switched on, and the measured finality and
watchability tables that decide whether a rail can be charged in a given mode.
A library cannot confirm any of that without the host it came from.

Private keys, transaction signing, refunds, payouts, custody, and treasury
movement are outside this contract. They have a different threat model from
receiving and proving a payment, and should not become an extra capability on a
merchant's read-only rail by accident.

If you want those too, the reference host is the Frappe/ERPNext app this package
was extracted from, which adds a Desk terminal, the `Crypto Sale` state machine,
a chain watcher, and optional ERPNext Sales Invoice booking.

## What is still assumed

Nineteen rounds of adversarial review went into the cookbook, the reference
example and the checks over both; ninety-seven findings reproduced and were
fixed. What follows is what survived — the boundary, stated so that a reader
knows where their own work begins. It is not a list of things nobody has
thought about; it is the list of things that cannot be settled from inside a
read-only receiving library.

**Installed rail code is trusted code.** Entry-point discovery imports plugins
into your process. Conformance checks their structure and the shape of what
they return; it does not sandbox them and cannot prove their observations
honest.

**The provider is trusted to be complete and available.** Genesis-hash and
chain-id checks stop a wrong-network configuration. They do not stop a
correctly identified indexer from omitting history, stalling, or returning
consistent lies.

**Finality is a policy, not a proof.** Confirmation depths and finalized tags
bound reorg exposure; they do not make a transaction irreversible. The depths
documented here are test-flow policies, not mainnet advice.

**The merchant is trusted to control the keys.** Address validation proves
format and sometimes network, never ownership — and a BIP-32 public key cannot
prove its own derivation path, so the operator owns the choice of account
branch and the wallet's gap limit.

**Allocator state is trusted to be durable and never rolled back.** The
identity fingerprint catches a different key, rail or mode. It cannot tell the
current file from an older valid copy of itself.

**Every sale is assumed to have an address of its own, used once.** Where a
rail has no unconditional per-sale binding, sequential reuse is unsafe too: an
old instruction stays payable, so a shared recipient may back one sale for its
entire lifetime.

**The payer's wallet is trusted to follow the network it was told.** BIP-21
does not encode Testnet 4 and Solana Pay does not encode a cluster. Receiving
funds proves payment against an instruction — never who the payer was.

**Your host is assumed to supply durable, atomic storage and real concurrency
control.** Sales, baselines, leases, claims and outcomes must survive a
restart; claiming `(rail, recipient, transaction_id)` and writing the terminal
state must be one transaction; writes and releases must carry the current
fencing token.

**Clocks and timestamps are trusted enough for expiry policy.** The window and
the maturation grace are yours. Late payments, part payments, overpayments and
refunds need a staffed workflow: the example never reuses a late-payment
address, and it never goes back for the money either.

**The example server is not production infrastructure.** Its sales, claims,
leases and review queue are in memory and a restart loses them. It has no
authentication, no operator workflow, and no database, and its allocator
assumes POSIX locking and a trustworthy filesystem.

**Independent price sources are trusted not to fail together.** HTTPS,
corroboration and a disagreement limit constrain one bad feed. They cannot
prove that several vendors are not wrong, or compromised, in the same
direction.

**Custody stays outside.** Keys, signing, refunds, payouts, treasury movement
and incident response are not what a watch-only receiving rail is for.

**The gates prove what they execute.** Deterministic checked examples, in the
documented order and environment; `raises` blocks check exception classes;
skipped blocks are syntax-checked only. Prose, live networks, provider honesty
and production concurrency are outside them. This is evidence, not an audit.

## Security model

Receiving addresses are checksum- and network-verified wherever their format
makes that possible. An address is never claimed as checked when a rail carries
no checksum or has no implemented format, and an unchecked address is refused on
mainnet. URI amounts must state the invoice exactly; mainnet prices require at
least two agreeing HTTPS feeds; network responses are size-bounded; and an HTTPS
read may redirect only to another HTTPS URL.

These checks verify form, network, amount, and transport. They do **not** prove
that the merchant controls a receiving key, that an external rate vendor is
honest, or that a host deployment has working chain watchers. The complete
guarantee and trust-boundary inventory ships in the source distribution as
`SECURITY.md`.

## Running the tests

Standard library only, no network, no framework, no test runner to install:

```bash
PYTHONPATH=src python -m unittest discover -s tests -t .
python3 tools/readme.py --wheel     # every recipe above, against the wheel
```

Every chain read is stubbed. A suite that needed a live indexer could not assert
the thing that matters most here — what happens when the indexer is gone — so
there is no network in it and there must never be one.

444 tests, no network, well under a second. Two installed-distribution checks
skip from a source-only run and execute against the built wheel.

Three of the checks are guards rather than tests of behaviour: one parses every
module and fails if anything outside the standard library is imported, one fails
if the strings `frappe`, `erpnext` or `bench` appear anywhere in the package,
and `tools/readme.py` fails if any example in this file stopped being true. All
three claims decay silently otherwise.

## Licence

MIT. Vendors `qrcodegen.py` from Project Nayuki (MIT), unchanged and with its
notice intact — so the symbol a customer scans is produced by the same encoder
across every surface. A QR that differs between two surfaces of the same
terminal is a defect that only shows up at the counter.
