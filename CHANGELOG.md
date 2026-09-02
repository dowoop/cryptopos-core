# Changelog

## 2.2.0

Four rounds of adversarial review of the cookbook, the reference example and
the gate over both found thirty-one defects. Nothing in the payment protocol
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
