# Changelog

## 2.2.0

Eleven rounds of adversarial review of the cookbook, the reference example
and the gate over both found eighty-three defects. Nothing in the payment protocol
changed; everything below is the library's testing surface, its documentation,
and the checks that keep them honest.

* Added `cryptopos_core.testing.MemoryRail`, a full `PaymentRail` backed by a
  dictionary instead of a provider, so a host can exercise double-credit,
  partial-read, misattribution and `needs-review` paths with no network and no
  funds. It ships in the wheel: the previous copy lived in `examples/`, which
  meant the README's first recipe could not be run by anyone who had installed
  the package rather than cloned it.
* `MemoryRail.readiness()` now refuses a configuration it cannot actually be
  driven with. It previously reported `chargeable=True` for `page=0`, where the
  observation loop every recipe prescribes can never terminate, and for a
  configuration with no `tip`, where `capture_baseline` then raised `KeyError`.
  A double that lies about readiness teaches the opposite of what readiness
  means.
* `MemoryRail.observe()` can report a transfer whose status could not be
  established, as an unconfirmed observation plus an unresolved id. The
  `needs-review` outcome had no way to be demonstrated before, so the README
  described it beside an example that produced `pending`.
* `tools/readme.py` no longer puts `examples/` on the path for a `--wheel` run,
  and takes `site-packages` off `sys.path` for it. That run now imports what
  the wheel contains and what the standard library provides, which is what it
  claimed to be checking.
* `tools/readme.py` inventories `# ->` markers with the TOKENIZER and fails on
  any that nothing checked. The previous line-regex missed a marker written
  above its statement, one nested inside a function, and one with nothing after
  the arrow — and fired on the characters appearing inside a string literal.
* The stale-wheel guard compares the wheel's module bytes against `src/`,
  refuses modules present in the wheel but deleted from `src/`, and compares
  the wheel's `Requires-Dist` and `Requires-Python` against `pyproject.toml`.
  A wheel whose metadata installs a dependency this tree no longer declares is
  no less stale for having identical source.
* Corrected the reference section that said core drives no rails, which stopped
  being true when a conformant `MemoryRail` moved into the package, and the
  recipe that called an elided skeleton "the whole of" that module.
* `examples/checkout_server.py`: a third adversarial round found eight more
  money-critical defects in the example itself, all fixed, each with a test
  that fails when the guard is removed (11 of 11 mutants killed).
  - Expiry read the clock and never the chain, so a payment confirming between
    the last poll and the deadline was recorded as "nothing received". The
    sweep now observes one final time and expires only on a complete read that
    found nothing; a failed read leaves the sale open, because a provider that
    will not answer is not evidence of non-payment.
  - The allocation counter was written non-atomically and read fail-open, so a
    crash mid-write handed the next sale index 0 again. It is now written
    through a temporary with `fsync` and an atomic rename, and unreadable state
    stops the shop rather than guessing zero.
  - Two processes each loaded the counter at start-up and each handed out the
    same index behind their own `threading.Lock`. Allocation re-reads the
    counter inside an interprocess file lock.
  - A shared recipient allowed one sale AT A TIME, which is not safe: the
    finished sale's QR is still payable and settles whoever holds the address
    next. It now allows one sale for the address's entire lifetime.
  - The index was allocated before the amount was validated, so refused
    requests silently consumed addresses toward the wallet's gap limit.
  - Watcher health was set only around the poll, so a failure anywhere else in
    the loop killed the thread while the server kept selling. The whole loop is
    supervised and its death stops the service.
  - A late worker was handed a fabricated `sighted_native` copied from
    `credited_native`, erasing the evidence a `needs-review` sale exists to
    show. The stored outcome is its own type carrying what was really seen.
* Both rail READMEs previously suggested returning a spent derivation index to
  a pool after a cooldown, to spare the wallet's gap limit. That is unsafe for
  the same reason: no finite cooldown makes an old payment request unpayable.
  They now say an index is spent the moment it is shown, and treat the gap
  limit as a constraint to plan for rather than to recycle around.
* A fourth round found five more, all fixed:
  - `MemoryRail.readiness()` collected every configuration problem into one
    string and attached it to all four capabilities, so it reported address
    validation unavailable "because there is no endpoint configured" while
    `validate_recipient` worked fine without one. Reasons now name only the
    capabilities they actually block.
  - The example's "one sale ever" for a shared recipient lived in memory, so a
    restart or a second process handed the same address to another customer.
    It is persisted with the allocation counter, read under the interprocess
    lock, and can only ever go from unused to used.
  - `tools/readme.py` read `# ->` claims out of COMMENT TOKENS instead of raw
    lines. The line regex read `"# -> not a comment"` inside a string as a
    claim, and let `1  # -> 1  # -> 2` pass by checking only the text after the
    last arrow. A comment carrying two arrows is now refused outright.
  - Claims are compared by type as well as value: `1  # -> True` and
    `1  # -> 1.0` both used to pass on Python equality while showing a reader
    a representation the code does not produce.
  - The wheel check compares `entry_points.txt` against pyproject, and the
    wheel's `Name` and `Version` against it — a wheel with a stale or missing
    entry point installs cleanly, matches every module byte, and provides no
    rail at all. `--wheel` also builds its `sys.path` from the standard library
    rather than filtering `site-packages` out of the existing one, which had
    left `PYTHONPATH` entries and editable installs reachable. Missing
    `tomllib` now refuses the run instead of silently skipping the comparison.
* A fifth round found eight more in the example, all fixed and all tested
  (18 guards, 18 mutants killed):
  - **A transaction id is not an exclusive payment identifier.** One chain
    transaction can carry outputs to several addresses, so an exchange batching
    its withdrawals pays two sales at once; claiming the bare id settled the
    first and left the second, whose customer really paid, pending forever.
    Claims are keyed on `(recipient, transaction_id)`, which is exact because
    every sale has its own address. The README's schema said `PRIMARY KEY
    (tx_id)` and now says the composite.
  - `pending` at the deadline was treated as "nothing arrived", so a confirmed
    part-payment was recorded `expired`, sighted zero, with the money on the
    chain. The window now closes into `needs-review` whenever anything was
    sighted, and a claim conflict is not an answer at all.
  - The static allocator wrote a cached `next_index` inside the lock and could
    roll a derived allocator's counter backwards, reissuing address zero.
  - The shared-address flag was committed at allocation, so one provider outage
    during `capture_baseline` permanently disabled checkout for an address no
    payer had ever seen. It is committed when the sale exists.
  - An absent state file was assumed to be a first run — indistinguishable from
    a different working directory or a restore that missed it. It now requires
    `CRYPTOPOS_INIT=1` once, and the schema is checked for exact JSON types
    rather than coerced.
  - A master extended key was accepted and derived at `0/index`, putting funds
    at a path no ordinary wallet scans. Depth 0 is refused.
  - The watcher was started before the server existed, so its shutdown branch
    found `None` and the process served anyway.
* A sixth round found six more:
  - Recipe 1 still defined the claimed set as "every id credited to *any*
    sale", directly contradicting the obligation added in the same round. The
    batched-payout case it was written for is now a runnable recipe, so the
    gate holds the two in agreement.
  - The static reservation was checked under the lock and persisted after the
    sale was created, so two processes could both see it free. It is reserved
    under the lock and released by `close()` when the sale never happens --
    which keeps the outage case fixed without reopening the race.
  - `MemoryRail.readiness()` reported settlement unavailable when the provider
    configuration was bad, but `settle` is a pure function of an intent and a
    batch and reads nothing. Only observation is blocked now; `chargeable` is
    still false, because it needs all four.
  - `tools/readme.py` compares a claim against `repr(value)` rather than
    type-and-value. `{"b": 2, "a": 1}  # -> {"a": 1, "b": 2}` and
    `-0.0  # -> 0.0` both passed as equal values a reader would never see. It
    immediately caught a claim in this README whose quoting was wrong.
  - The wheel's `Version` was never compared, because this project declares it
    dynamically and the check skipped an absent `[project].version`. It falls
    back to the version in the package.
  - The README embedded in the wheel's `METADATA` -- what PyPI and `pip show`
    display -- is compared with `README.md`, so the gate cannot approve a
    document different from the one the artefact publishes.
* A seventh round found seven more:
  - `MemoryRail` could not represent an unconfirmed transfer: every readable
    one was built `confirmed=True`, so `confs=0` raised out of the protocol's
    own validation. Money in the mempool, and money still maturing toward a
    confirmation depth, are the most ordinary pending states there are, and a
    double that cannot model them cannot test the paths that matter.
  - `MemoryRail.readiness()` refused observation without an `endpoint` while
    `observe` never read one, so the reason was invented. The endpoint is now
    genuinely required, which is what makes the readiness report true.
  - The README said an unreachable provider stops the rail "observing and
    settling" six lines above a checked claim showing settlement still ready.
  - The obligation list asserted that `needs-review` means money is involved.
    That is these rails' convention, not something `SettlementDecision`
    enforces; the wording now says so and tells a host to read the reason.
  - Claims are compared to `repr` EXACTLY. Collapsing whitespace on both sides
    let `"a  b"  # -> 'a b'` pass -- a difference a reader would copy.
  - The embedded-README comparison had the same lossy normalisation, and an
    absent description was exempt rather than a difference. Both fixed.
  - `--wheel` now runs in a fresh `python -I -S` subprocess. Rebuilding
    `sys.path` in-process left `sys.modules`, package `__path__`, import hooks
    and `sitecustomize` untouched; verified against a `sitecustomize` that
    preloads a fake package, which the isolated run no longer sees.
* An eighth round found ten more, and two of them were the example teaching the
  bug the README had just fixed:
  - The `Sales` docstring still showed `CREATE TABLE credited_tx (tx_id TEXT
    PRIMARY KEY, ...)`, the exact global key the batched-payout case defeats,
    and the start-up message still offered a fixed recipient as "one sale at a
    time" after that model had been rejected.
  - The claim key gained the rail: transaction ids and address strings are
    per-network, so a database serving two rails can collide on both.
  - **Expiry is a cutoff on the payer, not on the chain.** Money that arrived
    in time but had not matured -- one confirmation where the rail wants three,
    or a Bitcoin transfer waiting on a twenty-minute median block -- was made
    terminally `needs-review` at the deadline, so a payment two blocks from
    settling never settled. Sighted money now goes on being polled through a
    maturation grace period.
  - A pending decision can name creditable money; `poll_once` overwrote it with
    zero and the review record dropped it, so a reviewer was told 50 was
    sighted and nothing was creditable when 50 was.
  - Validating the amount only changed an attacker's payload from 0 to 1: valid
    unpaid sales still walked the wallet past its gap limit. The allocator
    tracks the run of addresses issued since the last payment and REFUSES new
    sales past a limit, which is the backpressure the README asked for.
  - A watcher that died while a request sat inside `capture_baseline` still
    handed that customer a live QR. Health is re-checked after the sale exists,
    and the sale goes straight to review rather than out of the door.
  - `MemoryRail` now reports the creditable amount on a pending decision, so a
    host can test part payments at all.
* A ninth round found twelve more:
  - **The backpressure counter measured the wrong quantity.** A wallet's gap
    limit is about consecutive UNUSED indices after the highest used one; the
    counter measured allocations since the last payment, so a late payment at
    index 0 reset it to zero while indices 1..n stayed unused and the real run
    kept growing. It tracks `highest_paid` now, and a state file written before
    that field existed is refused rather than read as "nothing unused".
  - **Expiry did not establish when a payment arrived.** `MemoryRail` had no
    notion of block time, so a transfer added after a sale's deadline settled
    normally and the README's "expiry is a cutoff on the payer" described
    something that never happened. A scripted transfer can now carry `at`, and
    a transfer that landed after the window is sighted and never creditable.
  - **A terminal write could beat a better-informed one.** Two workers reading
    the chain a block apart disagree, and a conditional write makes the FIRST
    one win: a `needs-review` from a stale read beat a `settled` from a fresh
    one and the paid sale never settled. A per-sale lease means one worker owns
    a sale from observation through to its terminal write.
  - An unresolved read was terminal. "I could not find out" is the absence of a
    decision, so one transient provider hiccup made a sale a person had to
    rescue; it stays pending and is asked again. `needs-review` is now
    demonstrated with the case that really is terminal -- money that arrived
    after the window closed.
  - `create_request` is checked against its intent before the URI is shown. It
    is the string the customer's money follows, and a rail returning a cached
    request would send it to another sale's address.
  - Sale ids use the full uuid4 rather than 48 bits of it, and a duplicate is
    refused instead of silently replacing the sale it collided with.
  - Three limits the example cannot close are now stated in the README rather
    than implied away: a BIP-32 key cannot prove its own derivation path, a
    restored older state file is valid JSON that rolls the allocator backwards,
    and late money is never misattributed but is not recovered either.
* A tenth round found six more:
  - **The request check verified metadata, not the instruction.** Comparing a
    `PaymentRequest`'s rail, recipient and amount passes for a request whose
    URI names an entirely different address -- and the URI is what the
    customer's money follows. The example now checks the URI names the address,
    or the component, the money must go through, and says plainly that past
    that it trusts the rail to encode its own amount honestly.
  - **The lease was not exclusive.** It held a deadline and no owner, so a
    worker whose observation outlived its lease released the lease its
    successor held, and every terminal method accepted the write because none
    asked who owned the sale. Leases carry a fencing token now, and `record`,
    `review` and `expire` refuse a write that does not hold it.
  - **A confirmed transfer with no arrival time was credited as timely**, so a
    payment made after the window settled simply because the script did not say
    when it landed. `MemoryRail` refuses to script one.
  - `highest_paid` advanced only on a settled sale, so a part payment, a late
    one, or one under review left its address looking unused and the gap
    counter over-reported. Any sighted money marks the address used.
  - The mid-request health failure recorded "the window closed with 0 sighted"
    for a sale seconds old, and committed a static deployment's one permitted
    allocation for a QR nobody was shown. It records what actually happened and
    releases the reservation.
  - The wheel check picked `wheels[-1]` from a directory that may hold several;
    ambiguity is refused rather than resolved. And the `- why` text after an
    exception name in a `raises` block is documented as a note for the reader
    rather than something the gate reads.
* An eleventh round found three more, and the first could divert a payment:
  - **Substring containment is not verification.** The URI check asked whether
    the recipient appeared anywhere in the URI, so
    `memory:mem1attacker?note=mem1merchant` passed and the QR paid the
    attacker while the sale watched the merchant. The destination is parsed per
    scheme now -- BIP-21 and ERC-681, including a token transfer's `address=`
    parameter -- and a scheme with no parser is refused rather than guessed.
  - **The fence was a convention, not an invariant.** `record`, `review` and
    `expire` accepted a tokenless write whenever no lease happened to be
    installed, so a host copying the README's worker loop -- which did not
    lease at all -- reintroduced the race the lease was added to remove. The
    token is required, and the cookbook's worker loop and SQL now show leasing,
    fencing and release.
  - `highest_paid` still did not advance for a pending part payment, because
    the pending branch returns before any terminal write. It advances on
    CONFIRMED money in the batch instead -- and only confirmed, since an
    unconfirmed transfer can be replaced and repeated ephemeral ones would
    otherwise walk the allocator past the wallet's recovery gap.

## 2.1.1

Documentation and packaging only; no behaviour changed.

* README.md is now a cookbook: nine task-shaped recipes covering one payment
  end to end, a web checkout, QR rendering, pricing, address refusal, rail
  selection, the three settlement outcomes, writing a rail, and testing a host
  with no chain. The reference material it had before is kept below them.
* Added `examples/checkout_server.py`, a complete stdlib checkout — form, QR,
  status endpoint, background watcher — that runs with no chain, no funds and
  no configuration, and points at a real rail through three environment
  variables. Added `examples/memory_rail.py`, the scripted rail it and the
  README recipes run on.
* Added `tools/readme.py`, which executes every Python example in README.md and
  fails if a `# ->` claim is untrue. `--wheel` runs them against `dist/*.whl`
  and refuses a wheel whose version does not match this tree, so a stale
  artefact cannot report an old package green.
* Corrected the package docstring, which shipped two false claims in 2.1.0:
  `register_builtins()` returns six rails and not twelve, and the chain reader
  moved to `cryptopos_rail_ootle.chain` in 2.0.
* `examples/` and `tools/` now ship in the sdist, so the files README.md links
  to are present in the distribution.

## 1.1.0

The package is now an installable payment-rail kernel rather than only a set
of point-of-sale helpers.

* Added the `PaymentRail` protocol, concrete `Network` and `Asset` identities,
  immutable payment/observation/settlement values, entry-point discovery, a
  built-in registry, readiness reporting, and host-side conformance checks.
* Added complete, provider-verified testnet rails for Bitcoin Testnet 4,
  Sepolia ETH, Sepolia USDC, and Amoy USDC.
* Bound recipient baselines and provider observations to the exact payment
  intent, recipient, provider, and starting chain position before settlement.
* Bounded EVM observation is resumable and cumulative rather than becoming
  permanently unavailable after a long invoice or provider outage.
* Settlement returns every credited transaction ID for atomic host claims and
  routes payments after the host-supplied expiry to review.
* ERC-20 observations now verify and return the canonical transfer-block time.
* EVM readiness exercises the actual full-block or token-log method required by
  the rail, and Monero no longer claims Stagenet address validation before the
  validator can express that network.
* Added a truthful twelve-rail catalog. Request-only and observation-only
  adapters say exactly which capability is unavailable rather than simulating
  a successful charge path.
* Split Ootle payment observation from loyalty policy reads. The payment rail
  consumes the indexer's vault-filtered deposit SSE replay, carries its
  monotonic event ID as an exact resumable cursor, attributes exact amounts to
  transaction IDs, uses UTC `finalized_at` for expiry, and settles committed
  deposits without a confirmation-depth gate. Its request remains an account
  address with an explicit notice because no registered Ootle payment URI
  exists.
* Demo payment requests now use test deployments, addresses, chain IDs, and
  Esmeralda authorities. Only explicit `mainnet` selects mainnet identity.
* Hardened oversized address input, canonical Monero prefix encoding, hostile
  finite price values, lossy policy integers, provider response bounds, HTTPS
  endpoints, network identity checks, ambient provider proxies, and vault-map
  traversal.

## 1.0.0

The release that hardened the original helper APIs at their money boundaries.
It did not make every catalog rail chargeable or remove the host and provider
trust boundaries documented in `SECURITY.md`. Everything below is a behaviour
change; four of them are breaking, and each one breaks in the direction of
refusing rather than proceeding.

### Addresses are checked now (new `addresses` module)

Nothing verified a receiving address before this release. `build_uri` embedded
whatever string it was handed, so a mistyped or wrong-network address produced
a perfectly scannable QR pointing at a key nobody holds.

* Bech32 and Bech32m (BIP-173 / BIP-350), Base58Check, EIP-55, Monero's
  block-based base58 with its Keccak checksum — all implemented against
  published vectors, all pure stdlib.
* **Network binding.** A testnet address on a mainnet sale is refused, and the
  refusal says which way round it is. Bitcoin, Dash, Zcash and Monero encode
  their network; EVM and Solana do not, and this module says so rather than
  implying a check it did not perform.
* **Three-valued verdict** — `ok`, `refused`, `unchecked`. `unchecked` is not
  a soft `ok`: Solana carries no checksum and Tari's format is unspecified, so
  claiming those were verified would make the whole verdict worthless.
* Ships `_keccak.py`, because `hashlib.sha3_256` is NIST SHA-3 and EIP-55
  needs original Keccak-256. They differ by one padding byte, which is enough
  to make every checksum wrong.

### Truncated URI amounts are refused (BREAKING)

A decimal-amount URI carries the display form, which truncates. Invoicing a
Solana sale through `rates.native_for` produced a QR asking for 73266000
lamports against an invoice of 73266666 — the customer pays what they were
shown, the sale sits 666 short of itself, and no amount of waiting fixes it.

* `build_uri` raises `AmountNotRepresentable` rather than emitting such a URI,
  and names the amount to invoice instead.
* New `rails.invoice_amount` is the charge-path entry point; it satisfies the
  invariant by construction. `rails.is_exactly_displayable` and
  `rails.representable_amount` expose the check.
* `rates.native_for` is unchanged and is now documented as the primitive it
  always was — correct for arithmetic, not for invoicing.

### Real-money pricing is held to three extra rules (BREAKING)

In `mainnet`, `quote()` now refuses to:

* price from `DEMO_MICROCENTS` — a hardcoded $64,000 BTC may price a demo and
  never real money;
* price from a single feed — one endpoint answering is not corroboration;
* price when feeds disagree by more than `MAX_FEED_SPREAD` (2%).

All three raise `RateUnavailable` or the new `FeedsDisagree`, **which
subclasses it** — a host already catching `RateUnavailable` keeps refusing
correctly with no change. Other modes are unaffected.

Also: three feeds instead of two, median instead of mean, prices parsed as
`Decimal`, and a new `quote_detailed()` carrying the time, the per-feed prices
and the measured spread.

### Policy reads refuse insecure transport (BREAKING)

* `OotleReader` refuses a non-`https://` indexer unless `allow_insecure=True`.
* Redirects from https to http are refused outright — without that the scheme
  check is decoration, since urllib follows a downgrade without comment.
* `promise()` facts now carry `indexer`. The default indexer is a **testnet**
  one (no mainnet policy tier is published), so a surface that shows a ceiling
  without its provenance is dropping it rather than not being given it.
* The module reaches the network through one seam, `chain._urlopen`.

### Fixed

* Non-finite feed values (`NaN`, infinities) are discarded instead of escaping
  the documented error model.
* Feed requests run concurrently, so quote latency is bounded by the slowest
  vendor timeout rather than the sum of all vendor timeouts.
* HTTPS price-feed redirects cannot downgrade to plaintext.
  Redirect schemes are normalized and every non-HTTPS destination is refused,
  including uppercase `HTTP` and non-web schemes.
* Monero validation binds each standard, integrated, and subaddress prefix to
  its exact decoded length. A checksum-valid arbitrary blob is not an address.
* Feed and indexer bodies have explicit size ceilings, so a broken endpoint
  cannot choose the terminal's memory use. Indexer URLs also refuse embedded
  credentials, queries, fragments, and non-HTTP schemes.
* Non-positive, boolean and lossy floating-point amounts/rates are refused at
  every public conversion and URI boundary.
* Invalid asset identifiers and unsupported URI rails now raise documented
  `CryptoPosError` subclasses rather than leaking built-in exceptions.
* Solana sale references are verified as 32-byte public keys before they enter
  the query string.
* Transparent Zcash URIs no longer carry a memo; ZIP-321 requires parsers to
  reject that combination. Their fresh address is the sale binding.
* Unknown mode strings are refused everywhere instead of being interpreted
  differently by pricing, address and URI code.
* Policy reads preserve malformed-vault-versus-zero balance semantics and
  remain total across malformed response headers and configuration types.
* The rail table and its rows are immutable after import.
* `USDC_ON_POLYGON` was not EIP-55 valid — a lowercase `c` where the checksum
  wants `C`. The bytes were right, so transfers would have worked; the point
  is that a hand-transcribed address nothing verified is how the same slip
  reaches a recipient address, where it is not recoverable. Every EVM address
  in the rails table is now checked by the suite.
* `make fmt` would rewrite 774 lines of the vendored `qrcodegen.py`. The
  linter honoured its exclusion; the formatter reads a different list.

### Compatibility

`build_uri(..., strict=False)` restores permissive **address** behavior off
mainnet. It never disables mode, amount, representability, or sale-binding
checks. **Mainnet ignores it** — a flag that skips the last address check before
real funds move is one that will eventually be passed by accident.

## 0.1.0

Extracted from the Frappe app: `rates`, `qr`, `chain`, `errors`, and the
vendored `qrcodegen`. Later joined by `rails` (12 rails, 8 families) and
`uri` (one branch per scheme).
