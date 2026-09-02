#!/usr/bin/env python3
"""A complete crypto checkout in one stdlib file.

	CRYPTOPOS_INIT=1 python3 checkout_server.py   # scripted rail, no chain, no funds
	python3 checkout_server.py                    # every run after the first

	CRYPTOPOS_RAIL=bitcoin:testnet4/native:btc \
	CRYPTOPOS_ENDPOINT=https://mempool.space/testnet4/api \
	CRYPTOPOS_XPUB=tpub... python3 checkout_server.py

`CRYPTOPOS_INIT=1` is required exactly once, to create the address-allocation
file. Afterwards its absence means something is wrong -- a different working
directory, an unmounted volume, a restore that missed it -- and starting from
zero would reissue receiving addresses that are already live.

Nothing here is Flask-, Django-, or FastAPI-specific: the only framework
contact points are "read a request" and "write a response". Everything between
them is the five-call rail protocol, and it is the same five calls in any host.

The five obligations marked (1)-(5) below are the ones in README.md, in the
same order. Each has cost this project real money at least once, and each has a
test in `test_checkout_server.py` that fails if the guard is removed.

WHAT THIS EXAMPLE IS NOT. It has no authentication, no operator workflow, and
no durable store beyond one small file for the derivation high-water mark. The
review page is a demonstration that `needs-review` needs somewhere to go, not a
back office. Copy the payment logic; build the rest for your own deployment.
"""

import contextlib
import fcntl
import hashlib
import html
import json
import os
import pathlib
import threading
import time
import uuid
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, parse_qsl, urlparse

from cryptopos_core import hd
from cryptopos_core.conformance import require_conformant
from cryptopos_core.errors import RailProviderError
from cryptopos_core.plugin import PaymentIntent
from cryptopos_core.registry import RailRegistry

WINDOW_SECONDS = 15 * 60
#: How long money that arrived IN TIME may go on maturing after the window
#: closes. Expiry is a cutoff on the payer, not on the chain: a transfer made
#: honestly at minute 14 still needs its confirmations, and on a rail that
#: settles at three of them, or at Bitcoin's twenty-minute median block, that
#: happens after the sale's clock has run out.
MATURATION_GRACE_SECONDS = 60 * 60
POLL_SECONDS = 5
#: The longest run of UNUSED indices a deployment may leave behind. A wallet
#: restoring from the seed stops after a run of unused addresses -- commonly
#: 20 -- so a shop that allocates past that puts real money where a restore
#: will not look. Refusing new sales is the backpressure; raising the wallet's
#: gap limit and raising this together is the other half.
UNPAID_RUN_LIMIT = int(os.environ.get("CRYPTOPOS_UNPAID_RUN_LIMIT", "15"))
STATE_FILE = pathlib.Path(os.environ.get("CRYPTOPOS_STATE", ".checkout-state.json"))

CLAIMED, CONFLICT, ALREADY_DECIDED = "claimed", "conflict", "already-decided"


class Unhealthy(Exception):
	"""The watcher died. New sales must stop; money must not be taken blind."""


HEALTH = {"watching": True, "why": ""}


# --------------------------------------------------------------------------
# Storage. This is the part you replace with your database.
# --------------------------------------------------------------------------
class Sales:
	"""Sales, plus the transaction ids already spent on one.

	(2) A CREDITED TRANSACTION ID MUST BE CLAIMED EXCLUSIVELY, AND THE CLAIM
	MUST BE PART OF THE SAME WRITE AS THE SALE STATE. `settle` is pure: it
	credits whatever you did not tell it was already spent. Reading the claimed
	set, settling, then writing looks safe and is not -- two workers read the
	same set before either writes, and one transfer settles two invoices.

	A lock around the READ does not fix it; the gap is between read and write.
	What fixes it is that claiming can FAIL. Here that is a check inside the
	lock; in your database it is

	    CREATE TABLE credited_tx (
	        rail_key  TEXT NOT NULL,
	        recipient TEXT NOT NULL,
	        tx_id     TEXT NOT NULL,
	        sale_id   TEXT NOT NULL REFERENCES sale(id),
	        PRIMARY KEY (rail_key, recipient, tx_id)
	    )

	and the INSERT that loses the race raises, rolling back the sale state with
	it. The constraint is the thing that saves you, not the lock.

	THE KEY IS NOT `tx_id` ALONE. One chain transaction can carry outputs to
	several addresses, so an exchange batching its withdrawals pays two sales
	at once; a global claim on the id settles the first and leaves the second,
	whose customer really paid, unpaid. Nor are ids and addresses global
	namespaces -- two networks can mint the same-looking strings -- so the rail
	belongs in the key too.

	The state transition is ALSO conditional. A worker that arrives late holds
	a decision computed from a stale snapshot -- typically `needs-review`,
	which carries no transaction ids at all, so the uniqueness constraint
	cannot catch it. Only `WHERE state = 'pending'` can.
	"""

	def __init__(self):
		self._lock = threading.Lock()
		self._sales = {}
		self._credited = set()

	def create(self, sale):
		with self._lock:
			if sale["id"] in self._sales:
				# Silently replacing a sale loses the earlier one's address and
				# its claim; refusing is the only safe answer to an id clash.
				raise ValueError(f"a sale with id {sale['id']} already exists")
			self._sales[sale["id"]] = sale
		return sale

	def get(self, sale_id):
		with self._lock:
			return dict(self._sales[sale_id]) if sale_id in self._sales else None

	def open_sales(self):
		with self._lock:
			return [dict(s) for s in self._sales.values() if s["state"] == "pending"]

	def in_review(self):
		"""Sales a person must decide. `needs-review` is not a status string:
		it is a queue, and money is sitting in it."""
		with self._lock:
			return [dict(s) for s in self._sales.values() if s["state"] == "needs-review"]

	def claimed_at(self, recipient):
		"""Transaction ids already spent AT THIS RECIPIENT.

		A transaction id alone is not an exclusive payment identifier: one
		chain transaction can carry outputs to several addresses, so an
		exchange batching its withdrawals pays two of your sales in one
		transaction. Claiming the bare id settled the first sale and left the
		second -- whose customer really paid -- looking unpaid.

		Scoping the claim by recipient is exact *because* every sale has its
		own address (obligation 1). Two sales can never share a
		(recipient, transaction) pair, and the same output can never be
		credited twice to the one sale that owns that address. A host that
		shares an address between sales gets the old, coarser protection,
		which is the same trade it already made.
		"""
		with self._lock:
			return frozenset(tx for (rail, at, tx) in self._credited
			                 if (rail, at) == (RAIL.key, recipient))

	def record(self, sale_id, decision, token):
		"""Claim the transactions and write the state, or do neither.

		Returns CLAIMED, CONFLICT (another sale owns one of these ids), or
		ALREADY_DECIDED (this sale is no longer pending). The caller needs the
		difference: a conflict may resolve on the next poll, whereas an already
		decided sale has no next poll and its stored state is the true answer.
		"""
		with self._lock:
			sale = self._sales[sale_id]
			if sale["state"] != "pending":
				return ALREADY_DECIDED
			# A TERMINAL WRITE REQUIRES THE LEASE, always. Accepting a
			# tokenless write whenever no lease happened to be installed made
			# the fence a convention that `_watch_one_pass` followed and a
			# reader copying the worker loop did not.
			if not self._holds(sale, token):
				return ALREADY_DECIDED
			mine = {(RAIL.key, sale["recipient"], tx) for tx in decision.transaction_ids}
			if self._credited.intersection(mine):
				return CONFLICT
			sale["state"] = decision.state
			sale["reason"] = decision.reason
			sale["credited_native"] = decision.credited_native
			sale["sighted_native"] = decision.sighted_native
			sale["transaction_ids"] = list(decision.transaction_ids)
			self._credited.update(mine)                       # claimed with the write
			return CLAIMED

	def review(self, sale_id, result, token):
		"""Hand a sale to a person, with what was actually seen."""
		with self._lock:
			sale = self._sales[sale_id]
			if sale["state"] != "pending" or not self._holds(sale, token):
				return False
			sale["state"] = "needs-review"
			sale["sighted_native"] = result.sighted_native
			sale["credited_native"] = result.credited_native
			sale["reason"] = result.reason or (
				f"the window closed with {result.sighted_native} sighted and "
				f"{result.credited_native} creditable")
			return True

	def lease(self, sale_id, now, seconds=30):
		"""Take exclusive ownership of a sale from observation to terminal write.

		A conditional write is not enough on its own. Two workers can read the
		chain at different moments and reach different conclusions, and then
		FIRST writer wins rather than best evidence: a worker that saw one
		confirmation writes `needs-review` while a worker that saw three is
		still deciding, and its `settled` loses the compare-and-set. The paid
		sale never settles. Whoever holds the lease is the only one who may
		observe and decide, which removes the disagreement rather than
		arbitrating it.
		"""
		with self._lock:
			sale = self._sales.get(sale_id)
			if sale is None or sale["state"] != "pending" or now < sale["leased_until"]:
				return None
			# A FENCING TOKEN, not just a deadline. A worker whose observation
			# outlived its lease came back and released the lease a SECOND
			# worker was holding, and every terminal method accepted its write
			# because none of them asked who owned the sale -- which is the
			# first-writer-wins problem the lease was added to remove.
			token = uuid.uuid4().hex
			sale["leased_until"], sale["lease"] = now + seconds, token
			return token

	def _holds(self, sale, token):
		return token is not None and sale.get("lease") == token

	def release(self, sale_id, token):
		with self._lock:
			sale = self._sales.get(sale_id)
			if sale is not None and self._holds(sale, token):
				sale["leased_until"], sale["lease"] = 0, None

	def expire(self, sale_id, now, token):
		"""Stop watching a sale whose window closed with nothing in it."""
		with self._lock:
			sale = self._sales[sale_id]
			if (sale["state"] != "pending" or now < sale["expires_at"]
					or not self._holds(sale, token)):
				return False
			sale["state"] = "expired"
			sale["reason"] = "the payment window closed with nothing received"
			return True


SALES = Sales()


# --------------------------------------------------------------------------
# Where each sale is paid.
# --------------------------------------------------------------------------
#: How to turn a derived key into an address, per network namespace. A rail
#: whose namespace is absent gets a REFUSAL, never a guess: deriving an EVM
#: address for an unknown chain produces something syntactically plausible
#: that the merchant holds no key for, which is the most expensive possible
#: way to be wrong.
DERIVATIONS = {
	"bitcoin": lambda key, testnet: hd.p2wpkh_address(key, "tb" if testnet else "bc"),
	"ethereum": lambda key, testnet: hd.evm_address(key),
	"polygon": lambda key, testnet: hd.evm_address(key),
	# The scripted rail's address space, so the demo exercises the same
	# per-sale derivation path as a real chain rather than a special case.
	"memory": lambda key, testnet: "mem1" + hashlib.sha256(key.public_key).hexdigest()[:16],
}


class Recipients:
	"""One address per sale, allocated once and NEVER reused.

	(1) TWO SALES MUST NOT SHARE A RECEIVING ADDRESS -- and "share" includes
	one after the other. A rail whose `binding_category` is
	`not-unconditional` credits every unclaimed, timely transfer it sees, so
	two OPEN sales at one address settle each other's money with no race at
	all. Reproduced here: a sale invoiced 100 settled on 350.

	Sequential reuse is no safer, and this is the part that is easy to get
	wrong. A payment instruction cannot be withdrawn. A customer who kept the
	QR from a finished sale can pay it tomorrow; if that address now belongs to
	a new sale, the transfer arrives after the new baseline, inside the new
	window, and settles the wrong invoice. Also reproduced here. So an index is
	spent forever the moment it is shown to anybody -- there is no cooldown
	long enough, because no finite time makes an old QR unpayable.

	THAT COSTS YOU GAP LIMIT, AND YOU PAY IT DELIBERATELY. A wallet restoring
	from the seed scans forward only until it meets a run of unused addresses,
	commonly 20 (BIP-44). Never reusing means abandoned checkouts burn indices,
	so a busy shop must keep the watching wallet's gap limit above its
	unpaid-sale run length, and must keep `_next` DURABLE -- which is why it is
	written to a file here. Losing it restarts allocation at zero and hands a
	live address to a second sale, which is failure (1) again by another route.
	Backpressure is the honest remedy if you cannot raise the gap limit; reuse
	is not.
	"""

	def __init__(self, rail, xpub, shared_recipient):
		self._rail = rail
		# WHOSE COUNTERS ARE THESE? The file records how far one allocator has
		# gone. Rotating the xpub, or pointing the same directory at another
		# rail, used to inherit those numbers: the new account has no history
		# at all, and the old `highest_paid` told the guard its unused run was
		# short when it was the whole range. A fingerprint of the key, the rail
		# and the mode makes that a refusal.
		self._identity = hashlib.sha256(
			f"{xpub or ''}|{rail.key}|{'derived' if xpub else 'shared:' + shared_recipient}"
			.encode()).hexdigest()[:16]
		self._account = hd.parse_extended_key(xpub) if xpub else None
		if self._account is not None and self._account.depth == 0:
			# A MASTER KEY IS NOT AN ACCOUNT KEY. Deriving `0/index` from it
			# gives addresses at a path no ordinary wallet scans, so the money
			# arrives somewhere the merchant cannot see. Both rail READMEs warn
			# about it; accepting it here would make the warning decorative.
			raise SystemExit(
				"CRYPTOPOS_XPUB is a master key (depth 0). Use the ACCOUNT key your wallet "
				"exports -- m/84'/1'/0' for testnet segwit, m/44'/60'/0' for EVM -- so that "
				"0/index is a path your wallet actually watches.")
		if self._account is not None:
			# AND THAT IS ALL THIS CAN PROVE. A BIP-32 key does not carry its
			# own path, so nothing here can tell an account key from another
			# branch at the same depth. Refusing depth 0 removes the one case
			# that IS provable; the rest is the operator's, and getting it
			# wrong means money at addresses their wallet never scans. Say the
			# depth out loud so a mistake is at least visible.
			print("derivation: 0/index under a depth-"
			      + str(self._account.depth)
			      + " key -- check this is the account branch your wallet watches")
		self._shared = shared_recipient
		self._lock = threading.Lock()
		self._open = {}
		self._committed = set()
		stored = self._read_state()
		# PERSISTED, like the index. "One sale ever" that forgets on restart is
		# "one sale per process", and the second process hands the same address
		# to a second customer while the first one's QR is still payable.
		#
		# Reading it here is defensive rather than load-bearing: `allocate`
		# re-reads it under the interprocess lock, which is what actually makes
		# the refusal correct when two processes race. Changing this line alone
		# therefore breaks no test, and that is expected rather than a gap.
		self._static_spent = stored["static_used"]
		self._next = stored["next_index"]
		if self._account is not None:
			namespace = rail.network.namespace
			if namespace not in DERIVATIONS:
				raise SystemExit(
					f"no address derivation is defined for the '{namespace}' namespace. "
					f"Deriving one anyway would produce an address you hold no key for.")

	def _read_state(self):
		"""The next free index, or a refusal.

		ABSENT means first run and zero is genuine. UNREADABLE does not: a
		truncated write, a stale backup or a wrong schema would all hand out
		index 0 again, re-issuing addresses that are already live. Failing open
		here is the reuse failure with extra steps, so this stops instead.
		"""
		if not STATE_FILE.exists():
			# ABSENCE IS NOT PROOF OF A FIRST RUN. A different working
			# directory, an unmounted volume, a restore that missed the file,
			# or a tidy-up after an incident all look identical to it -- and
			# each would reissue index 0 over addresses that are already live.
			# Starting fresh is an operator's decision, so it must be said.
			if os.environ.get("CRYPTOPOS_INIT") != "1":
				raise SystemExit(
					f"{STATE_FILE} does not exist. If this really is a new deployment, start "
					f"once with CRYPTOPOS_INIT=1 to create it. If it is not, find the file: "
					f"allocating from zero would re-issue live receiving addresses.")
			return {"next_index": 0, "static_used": False, "highest_paid": -1,
			        "identity": self._identity}
		try:
			stored = json.loads(STATE_FILE.read_text())
			index, used = stored["next_index"], stored["static_used"]
			# NO DEFAULT. A state file written before this field existed says
			# nothing about which of its indices were used, and reading that
			# silence as "none unused" permits exactly the over-allocation the
			# field exists to stop. Refuse and let an operator supply it.
			if stored.get("identity") != self._identity:
				raise ValueError(
					f"these counters belong to allocator {stored.get('identity')!r}, and this "
					f"one is {self._identity!r} -- a different key, rail, or mode. Its indices "
					f"say nothing about this one's unused run; use a separate CRYPTOPOS_STATE")
			if "highest_paid" not in stored:
				raise ValueError("no 'highest_paid' recorded. An older state file cannot say "
				                 "how long its run of unused addresses is; set it by hand "
				                 "(-1 if nothing has been paid) once you know")
			paid = stored["highest_paid"]
			# EXACT TYPES. `int("12")` and `bool("false")` both succeed and
			# both mean something other than what the file said; a coerced
			# allocation counter is an allocation counter you do not know.
			if (type(index) is not int or index < 0 or type(used) is not bool
					or type(paid) is not int or paid < -1 or paid >= max(index, 1)):
				raise ValueError("next_index must be a non-negative JSON integer, "
				                 "static_used a JSON boolean, and highest_paid an integer "
				                 "from -1 up to next_index - 1")
			return {"next_index": index, "static_used": used, "highest_paid": paid,
			        "identity": self._identity}
		except Exception as exc:
			raise SystemExit(
				f"{STATE_FILE} is unreadable ({exc}). Refusing to allocate: assuming zero "
				f"would re-issue receiving addresses that may already be live.") from None

	def _read_counter(self):
		return self._read_state()["next_index"]

	def _save(self, state):
		"""Replace the stored state atomically and durably.

		`write_text` truncates first, so a crash mid-write leaves a file that
		parses as nothing -- and the reader above would then refuse to start.
		Write a temporary, flush it to the platter, rename over the original,
		then flush the directory so the rename itself survives.
		"""
		temporary = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
		with open(temporary, "w") as handle:
			# EVERY FIELD COMES FROM THE CALLER, which read the whole state
			# inside the lock. Writing a cached `self._next` let a static
			# allocator roll a derived allocator's counter backwards and
			# reissue address zero.
			handle.write(json.dumps({"next_index": state["next_index"],
			                         "static_used": state["static_used"],
			                         "highest_paid": state["highest_paid"],
			                         "identity": self._identity}))
			handle.flush()
			os.fsync(handle.fileno())
		os.replace(temporary, STATE_FILE)
		directory = os.open(STATE_FILE.parent or ".", os.O_RDONLY)
		try:
			os.fsync(directory)
		finally:
			os.close(directory)

	@contextlib.contextmanager
	def _across_processes(self):
		"""Serialise the read-modify-write against other PROCESSES.

		A `threading.Lock` reaches one interpreter. Two workers each loaded the
		counter at start-up, each locked their own object, and each handed out
		index 10. Your real answer is a database sequence or a locked allocator
		row; this is the file-lock equivalent for a single-host example.
		"""
		STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
		with open(str(STATE_FILE) + ".lock", "w") as handle:
			fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
			try:
				yield
			finally:
				fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

	@property
	def per_sale(self):
		return self._account is not None

	def _address(self, index):
		child = hd.derive_path(self._account, f"0/{index}")
		return DERIVATIONS[self._rail.network.namespace](child, self._rail.network.is_testnet)

	def allocate(self, sale_id):
		with self._lock:
			if not self.per_sale:
				# ONE SALE FOR THE ADDRESS'S WHOLE LIFETIME, not one at a time.
				# A finished sale's QR is still payable, so the next sale at
				# this address settles on the previous customer's money. There
				# is no sequential version of a shared address that is safe.
				with self._across_processes():
					state = self._read_state()
					self._static_spent = state["static_used"]
					if self._open or self._static_spent:
						raise ValueError(
							"this deployment has no per-sale address source, and its single "
							"recipient has already been used. An address that was shown to a "
							"payer can still be paid, so it can never back a second sale. "
							"Set CRYPTOPOS_XPUB.")
					# RESERVE IT HERE, under the same lock that just read it.
					# Checking under the lock and persisting later left a
					# window in which two processes both saw `false` and both
					# opened a sale at the one permitted address. The reserve
					# is rolled back by `close()` if the sale is never created,
					# which is what keeps a provider outage from permanently
					# disabling checkout.
					self._static_spent = True
					self._save({"next_index": state["next_index"], "static_used": True,
					            "highest_paid": state["highest_paid"]})
					# THE ROLLBACK IS NOT CRASH-SAFE, and cannot be while
					# `Sales` is in memory. If the process dies between here
					# and `SALES.create`, the one permitted use is durably
					# spent on a sale that never existed, and this deployment
					# refuses to open another. Recovery is an operator setting
					# "static_used" back to false in the state file, after
					# checking that no payer was ever shown the address. A host
					# with a durable sale table resolves it automatically:
					# reservation and sale go in one transaction.
				self._open[sale_id] = self._shared
				return -1, self._shared
			with self._across_processes():
				state = self._read_state()             # whole state, INSIDE the lock
				# CONSECUTIVE UNUSED INDICES AFTER THE HIGHEST PAID ONE. Counting
				# "allocations since the last payment" is a different number and
				# not the one a wallet restore cares about: a late payment at
				# index 0 reset that counter to zero while indices 1..n stayed
				# unused, and the run kept growing unseen.
				unused = state["next_index"] - 1 - state["highest_paid"]
				if unused >= UNPAID_RUN_LIMIT:
					# BACKPRESSURE, not a warning in a README. Every allocation
					# is permanent, so a caller opening sales it never pays
					# walks the wallet past its gap limit and hides real money
					# from a restore. Refusing is the only thing that stops it.
					raise ValueError(
						f"{unused} addresses after the last paid one are unused, and the "
						f"limit is {UNPAID_RUN_LIMIT}. Raise the watching "
						f"wallet's gap limit and CRYPTOPOS_UNPAID_RUN_LIMIT together, or wait "
						f"for a sale to be paid.")
				index = state["next_index"]
				self._next = index + 1
				# Durable BEFORE the address is shown.
				self._save({"next_index": self._next, "static_used": state["static_used"],
				            "highest_paid": state["highest_paid"]})
			address = self._address(index)
			self._open[sale_id] = address
			return index, address

	def paid(self, index):
		"""Record that THIS index received money, shortening the unused run."""
		with self._lock, self._across_processes():
			state = self._read_state()
			self._save({"next_index": state["next_index"],
			            "static_used": state["static_used"],
			            "highest_paid": max(state["highest_paid"], index)})

	def commit(self, sale_id):
		"""Mark this allocation as having become a sale a payer can see.

		The static reservation was already taken under the lock in `allocate`;
		this only stops `close` from releasing it.
		"""
		with self._lock:
			self._committed.add(sale_id)

	def close(self, sale_id):
		"""Forget an open sale, releasing an UNCOMMITTED static reservation.

		A derived index is never released -- it was shown, or might have been.
		A static reservation taken for a sale that was never created was shown
		to nobody, and holding it would let one provider outage disable a
		deployment for good.
		"""
		with self._lock:
			had = self._open.pop(sale_id, None)
			committed = sale_id in self._committed
			self._committed.discard(sale_id)
			if self.per_sale or had is None or committed:
				return
			with self._across_processes():
				state = self._read_state()
				self._static_spent = False
				self._save({"next_index": state["next_index"], "static_used": False,
				            "highest_paid": state["highest_paid"]})


RAIL, CONFIG, RECIPIENTS, SERVER = None, None, None, None


def load_rail():
	global RECIPIENTS
	registry = RailRegistry()
	registry.discover()                      # every installed cryptopos-rail-* package
	key = os.environ.get("CRYPTOPOS_RAIL")
	if not key:
		from cryptopos_core.testing import MemoryRail   # ships in the wheel; see recipe 9
		registry.register(MemoryRail())
		key = MemoryRail.key
	rail = registry.get(key)                 # raises RailNotInstalled, the honest answer
	config = {"endpoint": os.environ.get("CRYPTOPOS_ENDPOINT", "memory://")}
	if key.startswith("memory:"):
		config.update(DEMO_CHAIN)
	require_conformant(rail, config)         # capability claims must match readiness
	readiness = rail.readiness(config)
	if not readiness.chargeable:
		raise SystemExit(f"{key} cannot be charged here: {readiness.unavailable}")

	xpub = os.environ.get("CRYPTOPOS_XPUB")
	shared = os.environ.get("CRYPTOPOS_RECIPIENT", "")
	if not xpub and not shared:
		if key.startswith("memory:"):
			xpub = DEMO_XPUB
		else:
			raise SystemExit(
				"set CRYPTOPOS_XPUB for a derived address per sale, or CRYPTOPOS_RECIPIENT "
				"to run ONE sale, ever, at a fixed address. Not one at a time: a QR from a "
				"finished sale can still be paid, and would settle whichever sale holds "
				"that address next.")
	RECIPIENTS = Recipients(rail, xpub, shared)
	return rail, config


DEMO_CHAIN = {"tip": 60, "page": 20, "transfers": []}
#: BIP-32 test vector 1 at m/0H -- a published key with a published seed, and
#: depth 1 rather than the master, so the demo goes through the same
#: account-key check a real deployment does. Fine for a demo whose coins do not
#: exist, and never for anything else.
DEMO_XPUB = ("xpub68Gmy5EdvgibQVfPdqkBBCHxA5htiqg55crXYuXoQRKfDBFA1WEjWgP6LHhwBZeNK"
             "1VTsfTFUHCdrfp1bgwQ9xv5ski8PX9rL2dZXvgGDnw")


#: The chain id an ERC-681 URI must name, per EVM network. A URI that names
#: another one tells the wallet to pay on a different chain entirely, and the
#: destination address is identical on all of them.
EVM_CHAIN_IDS = {("ethereum", "sepolia"): "11155111", ("polygon", "amoy"): "80002"}

#: Query parameters a payment URI may carry, per namespace. A WHITELIST,
#: because some parameters do not decorate an instruction, they REPLACE it:
#: BIP-72's `r` tells a capable wallet to ignore the address and amount
#: entirely and fetch a PaymentRequest from a URL, which may name any output
#: it likes and need not be signed. The address in front of it is then
#: decoration, and the sale watches an address nobody was ever told to pay.
URI_PARAMETERS = {
	"memory": {"amount"},
	"bitcoin": {"amount", "label", "message"},
	# NO GAS FIELDS. ERC-681 lets a URI suggest `gasPrice` and `gasLimit`, and
	# a wallet that honours them will. A one-wei invoice carrying
	# `gas=21000&gasPrice=10**15` costs the customer twenty-one ETH in fees,
	# the merchant receives their one wei, and the sale settles perfectly --
	# the loss is real, enormous, and invisible to everything the host checks.
	# Let the wallet estimate its own fee.
	"ethereum": {"value"},
	"polygon": {"value"},
}

#: The URI scheme each namespace must use. `bitcoin:` and `ethereum:` addresses
#: do not overlap, but nothing stops a defective rail emitting the wrong one.
URI_SCHEMES = {"memory": "memory", "bitcoin": "bitcoin",
               "ethereum": "ethereum", "polygon": "ethereum"}


def _check_payment_identity(rail, intent, through, uri):
	"""Refuse a URI that instructs anything but THIS payment.

	A destination alone is not the payment. Checking only that let through
	`bitcoin:` for a memory rail, `@1` for a Sepolia sale -- mainnet, where the
	same address exists -- and a `transfer` call on some other token contract
	while the observer watched the configured one. In each case the payer
	follows one instruction and the sale watches another.

	This covers the rails the example knows. A namespace it does not know is a
	refusal, not a guess: an instruction nobody can read must not be shown to
	a payer.
	"""
	namespace = rail.network.namespace
	if namespace == "ootle":
		# No registered scheme: the "URI" is the account or component itself.
		if uri != through:
			raise ValueError(f"the payment address is {uri!r}, not {through!r}")
		return
	if namespace not in URI_SCHEMES:
		raise ValueError(f"no URI parser for the '{namespace}' namespace, so this payment "
		                 f"instruction cannot be checked before it is shown to a payer")
	scheme, _, rest = uri.partition(":")
	if scheme != URI_SCHEMES[namespace]:
		raise ValueError(f"the payment URI uses the {scheme!r} scheme, not "
		                 f"{URI_SCHEMES[namespace]!r}")
	target = rest.split("@", 1)[0].split("?", 1)[0].split("/", 1)[0]
	# ORDERED. An ERC-681 contract call is identified by its argument TYPES in
	# order, not by a bag of names: `?uint256=1&address=M&bytes32=..` describes
	# a different ABI call from `transfer(address,uint256)`, and `parse_qs`
	# cannot tell them apart because it throws the order away.
	arguments = parse_qsl(urlparse(uri).query, keep_blank_values=True)
	query = parse_qs(urlparse(uri).query)

	allowed = URI_PARAMETERS[namespace]
	if rail.asset.namespace == "erc20":
		allowed = {"address", "uint256"}
	unknown = sorted(set(query) - allowed)
	if unknown:
		raise ValueError(
			f"the payment URI carries {unknown}, which this host does not understand. "
			f"Some parameters replace the instruction rather than decorate it -- BIP-72's "
			f"`r` sends the wallet to fetch a different payment request entirely")

	if namespace in ("ethereum", "polygon"):
		wanted_chain = EVM_CHAIN_IDS.get((namespace, rail.network.reference))
		named_chain = rest.split("@", 1)[1].split("?", 1)[0].split("/", 1)[0] if "@" in rest else ""
		if wanted_chain is None:
			raise ValueError(f"no chain id known for {rail.key}, so its URI cannot be checked")
		if named_chain != wanted_chain:
			raise ValueError(f"the payment URI names chain {named_chain or 'none'}, "
			                 f"not {wanted_chain}")
		# The FUNCTION, exactly. `"/transfer" not in path` accepted
		# `/transferFrom` and `/transferAnything`: an ERC-681 path names the
		# contract call a wallet makes, and a prefix match names a different one.
		path = urlparse(uri).path
		function = path.split("/", 1)[1] if "/" in path else ""
		if rail.asset.namespace == "erc20":
			# The URI targets the CONTRACT and carries the payee as a parameter.
			if target.lower() != rail.asset.reference.lower():
				raise ValueError(f"the payment URI calls contract {target!r}, "
				                 f"not {rail.asset.reference!r}")
			if function != "transfer":
				raise ValueError(f"the payment URI calls {function or 'no function'!r}, "
				                 f"not 'transfer'")
			# `transfer(address,uint256)` and nothing else. Several `address=`
			# parameters would leave the payee to whichever the wallet read
			# first; an extra typed argument, or these two in the other order,
			# is a different call.
			names = [name for name, _value in arguments]
			if names != ["address", "uint256"]:
				raise ValueError(f"the payment URI's arguments are {names}, not "
				                 f"['address', 'uint256'] -- that is a different ABI call")
			paid = arguments[0][1]
		else:
			if function:
				raise ValueError(f"a native EVM payment URI must call no function, not "
				                 f"{function!r}")
			if query.get("address"):
				raise ValueError("a native EVM payment URI must not carry an `address` parameter")
			paid = target
	else:
		paid = target

	if paid != through:
		raise ValueError(f"the payment URI pays {paid!r}, not {through!r}. It is the "
		                 f"instruction, and the fields beside it are not proof of it")


def start_sale(amount_native):
	if not HEALTH["watching"]:
		raise Unhealthy(HEALTH["why"])
	# VALIDATE BEFORE ALLOCATING. An index is spent the moment it is taken, so
	# a caller posting `amount=0` in a loop could otherwise walk the wallet
	# past its gap limit without ever being shown an address.
	if not isinstance(amount_native, int) or amount_native <= 0:
		raise ValueError("amount must be a positive integer in the asset's smallest unit")
	sale_id = f"sale-{uuid.uuid4().hex}"
	index, recipient = RECIPIENTS.allocate(sale_id)
	try:
		return _open_sale(sale_id, amount_native, index, recipient)
	except Exception:
		RECIPIENTS.close(sale_id)            # the INDEX stays spent; only the slot is freed
		raise


def _open_sale(sale_id, amount_native, index, recipient):
	verdict, why = RAIL.validate_recipient(recipient)
	if verdict == "refused":
		raise ValueError(f"receiving address refused: {why}")

	# (3) CAPTURE THE BASELINE BEFORE THE PAYER SEES ANYTHING. It records the
	# chain position the sale starts from. Capture it late and a transfer that
	# arrived before this sale existed can be credited to it.
	baseline = RAIL.capture_baseline(recipient, CONFIG)
	now = int(time.time())
	intent = PaymentIntent(
		intent_id=sale_id,
		rail_key=RAIL.key,
		recipient=recipient,
		amount_native=amount_native,
		created_at_epoch=now,
		expires_at_epoch=now + WINDOW_SECONDS,
		baseline=baseline,
	)
	request = RAIL.create_request(intent)
	# TRUST, THEN VERIFY. This is the string the customer's money follows, and
	# a rail returning a cached or mismatched request would send it to another
	# sale's address while this one watches its own.
	if (request.rail_key, request.recipient, request.amount_native) != (
			intent.rail_key, intent.recipient, intent.amount_native):
		raise ValueError("the rail returned a payment request for a different payment")
	# AND THE URI ITSELF, because those three fields are metadata beside the
	# instruction rather than proof of it: a request naming the right recipient
	# while its URI names another address passes the tuple check and sends the
	# customer's money elsewhere. Where a rail pays THROUGH a component rather
	# than to an address (Ootle), that component is what must appear.
	through = baseline.payment_component or intent.recipient
	_check_payment_identity(RAIL, intent, through, request.uri)
	sale = SALES.create({
		"id": sale_id, "intent": intent, "uri": request.uri,
		"amount_native": amount_native, "state": "pending", "reason": "",
		"credited_native": 0, "sighted_native": 0, "transaction_ids": [],
		"expires_at": intent.expires_at_epoch, "leased_until": 0, "lease": None,
		"recipient": recipient, "index": index, "notice": request.payer_notice,
	})
	# The address is spent HERE: the sale exists and its instruction is about
	# to be shown. Committing at allocation meant one provider outage during
	# `capture_baseline` permanently disabled a static deployment.
	if not HEALTH["watching"]:
		# The watcher died while this request was inside `capture_baseline`.
		# Checking health once, on the way in, let a customer be handed a live
		# QR that nothing was left to watch. Nothing has been shown yet, so the
		# static reservation is NOT committed -- `close()` releases it -- and
		# the derived index is spent, which is the honest cost of having built
		# the request at all.
		SALES.review(sale_id, PollResult("pending", 0, 0, (), "the watcher stopped "
		                                 "before this sale was shown to anyone"),
		             SALES.lease(sale_id, int(time.time())))
		raise Unhealthy(HEALTH["why"])
	RECIPIENTS.commit(sale_id)
	return sale


def poll_once(sale, token):
	"""One full observation cycle for one sale, then a settlement decision."""
	intent = sale["intent"]

	# (4) OBSERVE IS BOUNDED. It returns what it could read in one provider
	# call; loop until the batch reports `complete`, then decide. Deciding on
	# a partial read is deciding on a partial payment.
	batch = RAIL.observe(intent, CONFIG)
	while not batch.complete:
		batch = RAIL.observe(intent, CONFIG, batch)

	# VALIDATE BEFORE ANY SIDE EFFECT. A batch built for another intent or
	# baseline used to advance this address's durable high-water mark before
	# `settle` rejected it, and the false checkpoint survived the restart.
	# `settle` checks this too, so removing the line changes nothing today --
	# it is here so that a future side effect placed above the settle call
	# cannot silently reintroduce the same defect.
	batch.require_intent(intent)

	decision = RAIL.settle(intent, batch,
	                       claimed_transaction_ids=SALES.claimed_at(sale["recipient"]))

	# WHAT MAKES AN ADDRESS "USED" IS THE RAIL'S OWN MATURITY RULE, and
	# `credited_native` is the one unambiguous statement that it was met.
	#
	# `confirmed` is not: it means only `confirmations > 0`, so a single
	# shallow block moved the high-water mark permanently and no reorg could
	# move it back, hiding a later payment behind a longer unused run than the
	# counter believed. "Terminal and sighted" is not either -- money can be
	# sighted, terminal and uncredited, and nothing in `SettlementDecision`
	# promises otherwise, so a host that reads durability out of a terminal
	# state is reading a convention rather than a guarantee.
	#
	# Being wrong in this direction only over-reports the unused run, which
	# costs an early refusal rather than hidden money.
	if decision.credited_native:
		RECIPIENTS.paid(sale["index"])
	if decision.state == "pending":
		# CARRY WHAT THE RAIL SAID. Overwriting `credited_native` with zero
		# threw away a confirmed part payment: a rail can report pending with
		# money already creditable, just not enough of it.
		return PollResult("pending", decision.credited_native, decision.sighted_native,
		                  (), decision.reason)

	outcome = SALES.record(sale["id"], decision, token)
	if outcome == CLAIMED:
		RECIPIENTS.close(sale["id"])
		return PollResult(decision.state, decision.credited_native, decision.sighted_native,
		                  decision.transaction_ids, decision.reason)
	if outcome == ALREADY_DECIDED:
		# Do not report this worker's stale view. The stored state is the answer,
		# and there is no next poll for a sale that has left `open_sales()`.
		stored = _stored_decision(sale["id"])
		return PollResult(stored.state, stored.credited_native, stored.sighted_native,
		                  stored.transaction_ids, stored.reason)
	# A CONFLICT IS NOT AN ANSWER. Another sale claimed one of these
	# transactions between our read and our write; the next poll recomputes
	# against the larger claimed set. Expiring on it would record money that
	# demonstrably arrived as "nothing received".
	return PollResult("pending", 0, decision.sighted_native, (),
	                  "a transaction was claimed by another sale", conflict=True)


@dataclass(frozen=True)
class PollResult:
	"""What one poll concluded, and enough about it to act at the deadline.

	`pending` is not one thing. It covers a confirmed part-payment, money still
	maturing toward its confirmation depth, and a decision that lost a claim
	race -- and the deadline must treat none of those as "nothing arrived".
	Expiring on the bare state recorded a sale with 50 units confirmed on the
	chain as `expired`, reason "the payment window closed with nothing
	received", sighted zero.
	"""

	state: str
	credited_native: int
	sighted_native: int
	transaction_ids: tuple
	reason: str
	conflict: bool = False


@dataclass(frozen=True)
class StoredOutcome:
	"""What a sale ALREADY decided, for a worker that arrived too late.

	Deliberately not a `SettlementDecision`. It reports `expired`, which is not
	a settlement state at all, and an earlier version filled `sighted_native`
	from `credited_native` -- so a `needs-review` sale that had sighted 250 and
	credited 0 was reported back as having sighted nothing, erasing the exact
	evidence the review exists to show.
	"""

	state: str
	credited_native: int
	sighted_native: int
	transaction_ids: tuple
	reason: str


def _stored_decision(sale_id):
	stored = SALES.get(sale_id)
	return StoredOutcome(stored["state"], stored["credited_native"], stored["sighted_native"],
	                     tuple(stored["transaction_ids"]), stored["reason"])


def demo_payer():
	"""DEMO ONLY. Stands in for a customer with a wallet and the real chain."""
	while True:
		time.sleep(8)
		for sale in SALES.open_sales():
			CONFIG["tip"] += 20
			CONFIG["transfers"].append({
				"id": f"tx-{sale['id'][-6:]}", "to": sale["recipient"],
				"amount": sale["amount_native"], "confs": 3,
				"height": CONFIG["tip"] - 5, "at": int(time.time()),
			})


def watcher():
	"""Scheduling is the host's job. In production this is your job queue.

	THE WHOLE LOOP IS SUPERVISED, not just the poll. Health used to be set only
	around `poll_once`, so a failure in listing sales, in expiry, or in the
	allocator killed this daemon thread while the server kept taking payments
	nothing was left to watch -- quieter than a crash and worse.
	"""
	try:
		_watch_loop()
	except BaseException as exc:                     # noqa: BLE001 - the point
		HEALTH.update(watching=False, why=f"{type(exc).__name__}: {exc}")
		print(f"FATAL: the watcher stopped: {exc!r}")
		print("  no new sales will be accepted, and the server is shutting down;")
		print("  sales already open have live QR codes that nobody is now watching.")
		if SERVER is not None:
			threading.Thread(target=SERVER.shutdown, daemon=True).start()
		raise


def _watch_loop():
	while True:
		_watch_one_pass(int(time.time()))
		time.sleep(POLL_SECONDS)


def _watch_one_pass(now):
	"""One sweep over every open sale. Separated so it can be tested."""
	for sale in SALES.open_sales():
		token = SALES.lease(sale["id"], now)
		if token is None:
			continue                      # another worker owns this one
		try:
			# LOOK ONE MORE TIME BEFORE CALLING IT UNPAID. A payment that
			# confirmed between the last poll and the deadline is money the
			# customer really sent; expiring without observing throws it
			# away silently. And if this read fails, we do NOT know the
			# sale was unpaid -- so the provider branch below leaves it
			# open rather than expiring it on ignorance.
			result = poll_once(sale, token)
			if result.state != "pending" or now < sale["expires_at"]:
				continue
			if result.conflict:
				continue                  # unresolved contention, not an answer
			if result.sighted_native:
				# MONEY ARRIVED IN TIME AND IS NOT CREDITABLE YET. Making that
				# terminal at the deadline loses payments that were about to
				# succeed: a transfer one confirmation deep on a rail that
				# wants three is timely, funded, and simply not mature. Keep
				# polling through the grace period; only then does a person
				# decide.
				if now < sale["expires_at"] + MATURATION_GRACE_SECONDS:
					continue
				if SALES.review(sale["id"], result, token):
					RECIPIENTS.close(sale["id"])
				continue
			# (see Recipients) The address is NOT recycled: the QR it issued
			# is still payable by whoever kept it.
			if SALES.expire(sale["id"], now, token):
				RECIPIENTS.close(sale["id"])
		except RailProviderError as exc:
			# A PROVIDER ERROR IS NOT A VERDICT -- and it is the only thing
			# this handler may swallow. Anything else repeats
			# deterministically, and retrying it forever turns a paid sale
			# into one nobody is told about.
			print(f"  watch {sale['id']}: provider unavailable: {exc}")
		finally:
			SALES.release(sale["id"], token)
		# Anything that is not a provider error propagates to `watcher`,
		# which stops the service. Retrying a deterministic fault forever
		# turns a paid sale into one nobody is ever told about.


# --------------------------------------------------------------------------
# HTTP. The only framework-shaped code in the file.
# --------------------------------------------------------------------------
def qr_svg(uri):
	"""The QR is a GRID, not markup -- draw it wherever you render."""
	from cryptopos_core.qr import modules_for

	grid = modules_for(uri)
	size, quiet = grid["size"], grid["quiet"]
	side = size + quiet * 2
	squares = "".join(
		f'<rect x="{x + quiet}" y="{y + quiet}" width="1" height="1"/>'
		for y, row in enumerate(grid["rows"])
		for x, module in enumerate(row) if module == "1"
	)
	return (f'<svg viewBox="0 0 {side} {side}" width="260" height="260" '
	        f'shape-rendering="crispEdges" role="img" aria-label="payment code">'
	        f'<rect width="{side}" height="{side}" fill="#fff"/>'
	        f'<g fill="#000">{squares}</g></svg>')


PAGE = """<!doctype html><meta charset="utf-8"><title>Checkout</title>
<style>body{{font:15px/1.5 system-ui,sans-serif;margin:3rem auto;max-width:34rem}}
code{{background:#f4f4f5;padding:.15em .4em;border-radius:3px;word-break:break-all}}
#state{{font-weight:600}}</style>
<h1>Pay {amount} {symbol}</h1>
{qr}
<p><code>{uri}</code></p>
<p>{notice}</p>
<p>Status: <span id="state">pending</span> <span id="why"></span></p>
<script>
setInterval(async () => {{
  const r = await fetch("/sales/{id}/state");
  const s = await r.json();
  document.getElementById("state").textContent = s.state;
  document.getElementById("why").textContent = s.reason ? "— " + s.reason : "";
}}, 2000);
</script>"""

FORM = """<!doctype html><meta charset="utf-8"><title>Checkout</title>
<style>body{font:15px/1.5 system-ui,sans-serif;margin:3rem auto;max-width:34rem}</style>
<h1>New sale</h1><form method="post" action="/sales">
<label>Amount in the smallest unit: <input name="amount" value="250"></label>
<button>Charge</button></form>
<p><a href="/review">review queue</a></p>"""


class Handler(BaseHTTPRequestHandler):
	def _send(self, code, body, content_type="text/html; charset=utf-8"):
		payload = body.encode()
		self.send_response(code)
		self.send_header("Content-Type", content_type)
		self.send_header("Content-Length", str(len(payload)))
		self.end_headers()
		self.wfile.write(payload)

	def do_GET(self):
		path = urlparse(self.path).path
		if path == "/":
			if not HEALTH["watching"]:
				return self._send(503, "<h1>Not taking sales</h1><p>The watcher stopped: "
				                       f"{html.escape(HEALTH['why'])}</p>")
			return self._send(200, FORM)
		if path == "/review":
			# (5) `needs-review` NEEDS SOMEWHERE TO GO. Everything here is
			# escaped: `reason` is assembled from provider data, and a review
			# page that interpolates it raw is an injection surface aimed at
			# the one person who looks at payment problems.
			rows = "".join(
				f'<li><code>{html.escape(s["id"])}</code> — invoiced {s["amount_native"]}'
				f' at <code>{html.escape(s["recipient"])}</code> — {html.escape(s["reason"])}</li>'
				for s in SALES.in_review())
			return self._send(200, "<!doctype html><meta charset=utf-8><title>Review</title>"
			                       "<h1>Needs a person</h1><ul>"
			                       + (rows or "<li>nothing</li>") + "</ul>")
		if path.startswith("/sales/") and path.endswith("/state"):
			sale = SALES.get(path.split("/")[2])
			if sale is None:
				return self._send(404, "{}", "application/json")
			return self._send(200, json.dumps({
				"state": sale["state"], "reason": sale["reason"],
				"credited_native": sale["credited_native"],
				"transaction_ids": sale["transaction_ids"],
			}), "application/json")
		if path.startswith("/sales/"):
			sale = SALES.get(path.split("/")[2])
			if sale is None:
				return self._send(404, "no such sale")
			return self._send(200, PAGE.format(
				amount=sale["amount_native"], symbol=html.escape(RAIL.asset.symbol),
				qr=qr_svg(sale["uri"]), uri=html.escape(sale["uri"]),
				notice=html.escape(sale["notice"]), id=html.escape(sale["id"])))
		return self._send(404, "not found")

	def do_POST(self):
		if urlparse(self.path).path != "/sales":
			return self._send(404, "not found")
		length = int(self.headers.get("Content-Length") or 0)
		fields = parse_qs(self.rfile.read(length).decode())
		try:
			sale = start_sale(int(fields.get("amount", ["0"])[0]))
		except Unhealthy as exc:
			return self._send(503, f"not taking sales: {html.escape(str(exc))}")
		except Exception as exc:
			return self._send(400, f"refused: {html.escape(str(exc))}")
		self.send_response(303)
		self.send_header("Location", f"/sales/{sale['id']}")
		self.end_headers()

	def log_message(self, *args):
		pass


def main():
	global RAIL, CONFIG
	RAIL, CONFIG = load_rail()
	binding = "a derived address per sale" if RECIPIENTS.per_sale else \
		"ONE shared address -- one sale for its whole lifetime, and unsafe for real money"
	print(f"rail {RAIL.key} -- {binding}")
	global SERVER
	# BEFORE the watcher starts. If the watcher died first, its shutdown branch
	# found SERVER still None, did nothing, and main went on to serve.
	SERVER = ThreadingHTTPServer(("127.0.0.1", 8099), Handler)
	threading.Thread(target=watcher, daemon=True).start()
	if RAIL.key.startswith("memory:"):
		threading.Thread(target=demo_payer, daemon=True).start()
		print("demo rail: a scripted payer settles each sale about 8s after you charge it")
	print("http://127.0.0.1:8099        (review queue: /review)")
	SERVER.serve_forever()


if __name__ == "__main__":
	main()
