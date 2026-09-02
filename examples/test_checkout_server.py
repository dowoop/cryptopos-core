"""The three ways this example lost money before, each now a test.

	python3 -m unittest discover -s examples -t examples

Every one of these failed against an earlier version of `checkout_server.py`.
They are here because an example that teaches an integration pattern is code
that other people run, and a defect in it is a defect in every host that
copied it.
"""

import contextlib
import io
import json
import os
import pathlib
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import checkout_server as app  # noqa: E402
from cryptopos_core.errors import RailProviderError  # noqa: E402
from cryptopos_core.plugin import SettlementDecision  # noqa: E402
from cryptopos_core.testing import MemoryRail  # noqa: E402


class Harness(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.addCleanup(self.tmp.cleanup)
		app.RAIL = MemoryRail()
		app.SALES = app.Sales()
		app.CONFIG = {"endpoint": "memory://", "tip": 60, "page": 20, "transfers": []}
		app.HEALTH.update(watching=True, why="")
		app.STATE_FILE = pathlib.Path(self.tmp.name) / "state.json"
		os.environ["CRYPTOPOS_INIT"] = "1"          # a genuinely fresh allocator
		self.addCleanup(os.environ.pop, "CRYPTOPOS_INIT", None)
		app.RECIPIENTS = app.Recipients(app.RAIL, app.DEMO_XPUB, "")

	def poll(self, sale):
		"""Poll as a worker must: holding the sale's lease from the observation
		through to the terminal write."""
		token = app.SALES.lease(sale["id"], int(time.time()))
		try:
			return app.poll_once(sale, token)
		finally:
			app.SALES.release(sale["id"], token)

	def pay(self, sale, amount, height, txid, at=None):
		app.CONFIG["tip"] = max(app.CONFIG["tip"], height + 5)
		app.CONFIG["transfers"].append({
			"id": txid, "to": sale["recipient"], "amount": amount,
			"confs": 3, "height": height, "at": at or (sale["expires_at"] - 60),
		})


class OneSaleOneCustomersMoney(Harness):
	def test_a_sale_does_not_settle_on_another_sales_payment(self):
		"""Two open sales, each paid its own amount, at a shared address, and
		the first to poll took BOTH transfers: settled 350 against a 100
		invoice while the customer who paid 250 went uncredited."""
		first = app.start_sale(100)
		second = app.start_sale(250)
		self.assertNotEqual(first["recipient"], second["recipient"],
		                    "two open sales were given the same address")
		self.pay(first, 100, 75, "tx-for-first")
		self.pay(second, 250, 95, "tx-for-second")

		one, two = self.poll(first), self.poll(second)
		self.assertEqual((one.state, one.credited_native), ("settled", 100))
		self.assertEqual((two.state, two.credited_native), ("settled", 250))


class NoSharedAddressWithoutRefusing(Harness):
	def test_a_second_sale_is_refused_when_addresses_cannot_be_derived(self):
		"""With no per-sale address source, opening a second sale is the
		unsafe state itself. Refusing is the only correct answer."""
		class Static:
			key = "bitcoin:testnet4/native:btc"

			class network:
				namespace, is_testnet = "bitcoin", True

		app.RECIPIENTS = app.Recipients(Static, None, "mem1static")
		self.assertFalse(app.RECIPIENTS.per_sale)
		app.start_sale(100)
		with self.assertRaises(ValueError):
			app.start_sale(250)

	def test_an_address_is_never_handed_to_a_second_sale(self):
		"""A payment instruction cannot be withdrawn.

		An earlier version of this example returned a finished sale's index to
		a pool after a cooldown, to spare the wallet's gap limit. A customer
		who kept the first QR could then pay it again, and because that
		transfer arrived after the SECOND sale's baseline and inside its
		window, the second sale settled on the first customer's money. No
		cooldown fixes it: no finite time makes an old QR unpayable.
		"""
		first = app.start_sale(100)
		self.pay(first, 100, 70, "first-paid")
		self.assertEqual(self.poll(first).state, "settled")

		second = app.start_sale(100)
		self.assertNotEqual(second["recipient"], first["recipient"])
		self.assertGreater(second["index"], first["index"])

		# The first customer pays the old QR again. It must reach nothing.
		self.pay(first, 100, 100, "first-paid-again")
		self.assertEqual(self.poll(second).state, "pending")

	def test_the_high_water_mark_survives_a_restart(self):
		"""Losing it restarts allocation at zero and hands a live address to
		a second sale, which is the reuse failure by another route."""
		first = app.start_sale(100)
		revived = app.Recipients(app.RAIL, app.DEMO_XPUB, "")
		self.assertGreater(revived.allocate("later")[0], first["index"])


class OneTransferOneInvoice(Harness):
	def test_two_workers_cannot_credit_the_same_transfer(self):
		"""The claimed set is read, then settled against, then written. Two
		workers that read it before either writes both settled on one
		transfer until claiming became something that can fail."""
		first = app.start_sale(100)
		second = app.start_sale(100)
		# Force the unsafe interleaving rather than wait for luck: both sales
		# are pointed at ONE transfer, and both read the claims before either
		# records. A shared address is refused above, so this is constructed.
		second["intent"] = second["intent"].__class__(
			second["intent"].intent_id, app.RAIL.key, first["recipient"], 100,
			second["intent"].created_at_epoch, second["intent"].expires_at_epoch,
			baseline=app.RAIL.capture_baseline(first["recipient"], {**app.CONFIG, "tip": 60}))
		second["recipient"] = first["recipient"]
		self.pay(first, 100, 75, "tx-contested")

		barrier = threading.Barrier(2)
		real = app.SALES.claimed_at

		def claimed_then_wait(recipient):
			seen = real(recipient)
			barrier.wait()
			return seen

		app.SALES.claimed_at = claimed_then_wait
		out = {}
		threads = [threading.Thread(target=lambda s=s: out.__setitem__(s["id"], self.poll(s)))
		           for s in (first, second)]
		for t in threads:
			t.start()
		for t in threads:
			t.join()

		settled = [d for d in out.values() if d.state == "settled"]
		self.assertEqual(len(settled), 1, "one transfer settled two invoices")

	def test_a_decided_sale_is_not_overwritten(self):
		"""At-least-once delivery means the same sale can be polled twice.

		The second worker's decision carries NO transaction ids -- it found
		them already claimed and returned `needs-review`. The claimed-set
		check therefore cannot stop it, and only the compare-and-set on the
		sale's state keeps a settled sale from being reopened. Removing that
		check must fail this test; it did not, until the decision under test
		was one the claimed set could not catch.
		"""
		sale = app.start_sale(100)
		self.pay(sale, 100, 75, "tx-once")
		self.assertEqual(self.poll(sale).state, "settled")

		late = SettlementDecision("needs-review", 0, 100, reason="a slower worker")
		token = app.SALES.lease(sale["id"], int(time.time()))
		self.assertEqual(app.SALES.record(sale["id"], late, token), app.ALREADY_DECIDED)
		self.assertEqual(app.SALES.get(sale["id"])["state"], "settled")
		self.assertEqual(app.SALES.get(sale["id"])["credited_native"], 100)


class MoneyMustArriveInTime(Harness):
	def test_a_transfer_after_the_window_is_not_credited(self):
		"""Expiry is a cutoff on the payer. Without a block time the example
		could not tell a timely payment from a late one, and a sale whose
		deadline had passed settled anyway."""
		sale = app.start_sale(100)
		self.pay(sale, 100, 71, "too-late", at=sale["expires_at"] + 1)
		result = self.poll(sale)
		self.assertEqual(result.state, "needs-review")
		self.assertEqual(result.credited_native, 0)
		self.assertEqual(result.sighted_native, 100)

	def test_late_dust_cannot_veto_a_paid_sale(self):
		"""Anyone who knows a public address can send one unit after the
		window. Asking about late money before asking whether the customer has
		paid let that dust end a complete, timely, matured payment in review,
		claiming nothing."""
		sale = app.start_sale(100)
		self.pay(sale, 100, 71, "paid-in-time", at=sale["expires_at"] - 100)
		self.pay(sale, 1, 120, "late-dust", at=sale["expires_at"] + 1)
		result = self.poll(sale)
		self.assertEqual(result.state, "settled")
		self.assertEqual(result.credited_native, 100)
		self.assertEqual(result.sighted_native, 101)
		self.assertEqual(result.transaction_ids, ("paid-in-time",))

	def test_an_unreadable_transfer_cannot_veto_a_paid_sale(self):
		"""Same shape: an unrelated read failure beside a complete payment."""
		sale = app.start_sale(100)
		self.pay(sale, 100, 71, "paid-in-time", at=sale["expires_at"] - 100)
		app.CONFIG["transfers"].append({
			"id": "unreadable-dust", "to": sale["recipient"], "amount": 1,
			"confs": 3, "height": 120, "at": sale["expires_at"] - 50,
			"unreadable": True})
		self.assertEqual(self.poll(sale).state, "settled")

	def test_a_transfer_inside_the_window_is_credited(self):
		sale = app.start_sale(100)
		self.pay(sale, 100, 71, "in-time", at=sale["expires_at"] - 1)
		self.assertEqual(self.poll(sale).state, "settled")


class TheGapLimitIsDefended(Harness):
	def test_a_late_payment_low_down_does_not_hide_the_unused_run(self):
		"""The invariant is consecutive unused indices after the highest PAID
		one. Counting allocations since the last payment let a late payment at
		index 0 reset the counter while indices 1..n stayed unused."""
		original, app.UNPAID_RUN_LIMIT = app.UNPAID_RUN_LIMIT, 3
		try:
			first = app.start_sale(100)
			for _ in range(2):
				app.start_sale(1)
			self.pay(first, 100, 71, "late-but-low")
			self.assertEqual(self.poll(first).state, "settled")
			# index 0 is paid; 1 and 2 are unused; one more reaches the limit.
			app.start_sale(1)
			with self.assertRaises(ValueError):
				app.start_sale(1)
		finally:
			app.UNPAID_RUN_LIMIT = original

	def test_valid_but_unpaid_sales_eventually_get_backpressure(self):
		"""Validating the amount only changed the attacker's payload from 0
		to 1. Every allocation is permanent, so a caller opening sales it
		never pays walks the wallet past its gap limit."""
		original, app.UNPAID_RUN_LIMIT = app.UNPAID_RUN_LIMIT, 3
		try:
			for _ in range(3):
				app.start_sale(1)
			with self.assertRaises(ValueError):
				app.start_sale(1)
		finally:
			app.UNPAID_RUN_LIMIT = original

	def test_a_confirmed_part_payment_marks_the_address_used(self):
		"""The sale is still pending -- 50 of 100 -- but the address demonstrably
		holds confirmed money, so it is not an unused address for a wallet
		restore. Advancing only on a terminal decision left it looking unused
		and the gap counter over-reported for good."""
		original, app.UNPAID_RUN_LIMIT = app.UNPAID_RUN_LIMIT, 3
		try:
			first = app.start_sale(100)
			self.pay(first, 50, 71, "half-of-it")
			self.assertEqual(self.poll(first).state, "pending")
			for _ in range(2):
				app.start_sale(1)
			self.assertIsNotNone(app.start_sale(1))    # index 0 counts as used
		finally:
			app.UNPAID_RUN_LIMIT = original

	def test_a_foreign_batch_does_not_mark_this_address_used(self):
		"""A batch built for another intent used to advance this address's
		durable high-water mark before settle rejected it, and the false
		checkpoint survived a restart."""
		mine = app.start_sale(100)
		theirs = app.start_sale(100)
		self.pay(theirs, 100, 71, "not-mine")
		foreign = app.RAIL.observe(theirs["intent"], app.CONFIG)
		before = app.RECIPIENTS._read_state()["highest_paid"]

		# A defective or hostile rail hands back a batch built for the OTHER
		# sale. It must be refused before anything durable moves.
		app.RAIL.observe = lambda *a, **k: foreign
		token = app.SALES.lease(mine["id"], int(time.time()))
		with self.assertRaises(Exception):
			app.poll_once(app.SALES.get(mine["id"]), token)
		self.assertEqual(app.RECIPIENTS._read_state()["highest_paid"], before)

	def test_a_LATE_shallow_confirmation_does_not_mark_the_address_used(self):
		"""A late transfer one block deep is terminal with money sighted and
		none credited, so "terminal and sighted" persisted exactly the shallow
		confirmation the depth rule exists to exclude."""
		app.RAIL.min_confirmations = 3
		original, app.UNPAID_RUN_LIMIT = app.UNPAID_RUN_LIMIT, 2
		try:
			first = app.start_sale(100)
			app.CONFIG["tip"] = 100
			app.CONFIG["transfers"].append({
				"id": "late-and-shallow", "to": first["recipient"], "amount": 100,
				"confs": 1, "height": 71, "at": first["expires_at"] + 1})
			self.poll(first)
			self.assertEqual(app.RECIPIENTS._read_state()["highest_paid"], -1)
			app.start_sale(1)
			with self.assertRaises(ValueError):
				app.start_sale(1)
		finally:
			app.UNPAID_RUN_LIMIT = original

	def test_only_credited_money_marks_the_address_used(self):
		"""A late transfer deep enough to be terminal still credits nothing,
		and `credited_native` is the one unambiguous statement that the rail's
		own depth gate was passed. Being conservative here costs an early
		refusal; being generous hides money behind a longer run than the
		counter believes."""
		original, app.UNPAID_RUN_LIMIT = app.UNPAID_RUN_LIMIT, 2
		try:
			first = app.start_sale(100)
			self.pay(first, 100, 71, "late-but-deep", at=first["expires_at"] + 1)
			result = self.poll(first)
			self.assertEqual((result.state, result.credited_native), ("needs-review", 0))
			self.assertEqual(app.RECIPIENTS._read_state()["highest_paid"], -1)
		finally:
			app.UNPAID_RUN_LIMIT = original

	def test_a_shallow_confirmation_does_not_mark_the_address_used(self):
		"""`confirmed` means only `confirmations > 0`. One shallow block moved
		the durable high-water mark permanently, and a reorg could not move it
		back -- hiding a later payment behind a longer unused run than the
		counter believed. What counts is money the RAIL was willing to credit,
		which is what passing its own depth gate means."""
		app.RAIL.min_confirmations = 3
		original, app.UNPAID_RUN_LIMIT = app.UNPAID_RUN_LIMIT, 2
		try:
			first = app.start_sale(100)
			app.CONFIG["tip"] = 100
			app.CONFIG["transfers"].append({
				"id": "one-block-deep", "to": first["recipient"], "amount": 100,
				"confs": 1, "height": 71, "at": first["expires_at"] - 60})
			result = self.poll(first)
			self.assertEqual((result.state, result.sighted_native), ("pending", 100))
			self.assertEqual(result.credited_native, 0)

			app.start_sale(1)
			with self.assertRaises(ValueError):
				app.start_sale(1)       # index 0 is NOT yet a used address
		finally:
			app.UNPAID_RUN_LIMIT = original

	def test_an_unconfirmed_sighting_does_not_mark_the_address_used(self):
		"""An unconfirmed transfer can be replaced or dropped, so letting one
		reset the counter would let repeated ephemeral transactions walk the
		allocator past the wallet's recovery gap."""
		original, app.UNPAID_RUN_LIMIT = app.UNPAID_RUN_LIMIT, 2
		try:
			first = app.start_sale(100)
			app.CONFIG["tip"] = 100
			app.CONFIG["transfers"].append({
				"id": "in-the-mempool", "to": first["recipient"], "amount": 100,
				"confs": 0, "height": 71, "at": first["expires_at"] - 60})
			self.assertEqual(self.poll(first).state, "pending")
			app.start_sale(1)
			with self.assertRaises(ValueError):
				app.start_sale(1)
		finally:
			app.UNPAID_RUN_LIMIT = original

	def test_a_payment_at_the_top_shortens_the_unused_run(self):
		original, app.UNPAID_RUN_LIMIT = app.UNPAID_RUN_LIMIT, 3
		try:
			for _ in range(2):
				app.start_sale(1)
			paid = app.start_sale(100)
			self.pay(paid, 100, 71, "a-real-payment")
			self.assertEqual(self.poll(paid).state, "settled")
			self.assertIsNotNone(app.start_sale(1))     # nothing after it is unused
		finally:
			app.UNPAID_RUN_LIMIT = original

	def test_an_old_state_file_without_the_field_is_refused(self):
		"""It cannot say how long its run of unused addresses is, and reading
		that silence as zero permits the over-allocation the field prevents."""
		identity = app.Recipients(app.RAIL, app.DEMO_XPUB, "")._identity
		app.STATE_FILE.write_text(json.dumps(
			{"next_index": 100, "static_used": False, "identity": identity}))
		with self.assertRaises(SystemExit) as refused:
			app.Recipients(app.RAIL, app.DEMO_XPUB, "")
		# Naming the field is not enough -- a bare KeyError does that. The
		# refusal has to tell an operator what to do about it.
		self.assertIn("set it by hand", str(refused.exception))


class TheDoubleModelsRealChainStates(Harness):
	def test_an_unconfirmed_transfer_is_sighted_but_not_credited(self):
		"""Money in the mempool, or still maturing toward a confirmation
		depth, is the most ordinary pending state there is. The double used to
		raise out of the protocol's own validation on `confs=0`."""
		sale = app.start_sale(250)
		app.CONFIG["tip"] = 100
		app.CONFIG["transfers"].append({
			"id": "in-the-mempool", "to": sale["recipient"], "amount": 250,
			"confs": 0, "height": 71, "at": sale["expires_at"] - 60})
		result = self.poll(sale)
		self.assertEqual(result.state, "pending")
		self.assertEqual(result.sighted_native, 250)
		self.assertEqual(result.credited_native, 0)

	def test_an_unconfirmed_payment_at_the_deadline_reaches_a_person(self):
		"""It is timely and fully funded and simply not mature. Recording it
		as 'nothing received' loses a payment that was really made."""
		sale = app.start_sale(250)
		app.CONFIG["tip"] = 100
		app.CONFIG["transfers"].append({
			"id": "still-maturing", "to": sale["recipient"], "amount": 250,
			"confs": 0, "height": 71, "at": sale["expires_at"] - 60})
		now = int(time.time())
		app.SALES._sales[sale["id"]]["expires_at"] = now - 1
		app._watch_one_pass(now + app.MATURATION_GRACE_SECONDS + 1)
		self.assertEqual(app.SALES.get(sale["id"])["state"], "needs-review")

	def test_money_that_arrived_in_time_is_allowed_to_mature(self):
		"""Expiry is a cutoff on the PAYER, not on the chain. A transfer one
		confirmation deep on a rail that wants three is timely and funded, and
		making it terminal at the deadline loses a payment about to succeed."""
		sale = app.start_sale(250)
		app.CONFIG["tip"] = 100
		unconfirmed = {"id": "maturing", "to": sale["recipient"], "amount": 250,
		               "confs": 0, "height": 71, "at": sale["expires_at"] - 60}
		app.CONFIG["transfers"].append(unconfirmed)
		now = int(time.time())
		app.SALES._sales[sale["id"]]["expires_at"] = now - 1

		app._watch_one_pass(now)
		self.assertEqual(app.SALES.get(sale["id"])["state"], "pending")

		unconfirmed["confs"] = 3                       # two blocks later
		app._watch_one_pass(now + 10)
		self.assertEqual(app.SALES.get(sale["id"])["state"], "settled")


class RefusesRatherThanGuesses(Harness):
	def test_an_unknown_namespace_gets_no_derived_address(self):
		"""Deriving an EVM address for an unknown chain yields something
		syntactically plausible that the merchant holds no key for."""
		class Exotic:
			key = "zcash:testnet/native:zec"

			class network:
				namespace, is_testnet = "zcash", True

		with self.assertRaises(SystemExit):
			app.Recipients(Exotic, app.DEMO_XPUB, "")

	def test_an_unwatched_service_stops_taking_sales(self):
		"""If the watcher dies, an HTTP server that keeps charging customers
		is worse than one that stops: nothing is left to notice their money."""
		app.HEALTH.update(watching=False, why="watcher died")
		with self.assertRaises(app.Unhealthy):
			app.start_sale(100)


class TheWindowCloses(Harness):
	def test_an_unpaid_sale_expires_instead_of_polling_forever(self):
		"""A sale nobody paid must leave `open_sales()`, or in shared mode it
		blocks every later sale and in derived mode it is watched forever."""
		sale = app.start_sale(100)
		token = app.SALES.lease(sale["id"], int(time.time()))
		self.assertTrue(app.SALES.expire(sale["id"], sale["expires_at"] + 1, token))
		self.assertEqual(app.SALES.get(sale["id"])["state"], "expired")
		self.assertEqual(app.SALES.open_sales(), [])

	def test_a_live_sale_does_not_expire_early(self):
		sale = app.start_sale(100)
		token = app.SALES.lease(sale["id"], int(time.time()))
		self.assertFalse(app.SALES.expire(sale["id"], sale["expires_at"] - 1, token))
		self.assertEqual(app.SALES.get(sale["id"])["state"], "pending")


class TheDeadlineDoesNotEatMoney(Harness):
	def test_a_payment_arriving_before_the_deadline_is_still_credited(self):
		"""Expiring without a final observation throws away a real payment.

		The watcher used to call `expire()` and `continue` the moment the clock
		passed, so a transfer that confirmed between the last poll and the
		deadline was never seen: the sale was recorded `expired` with the money
		sitting on the chain.
		"""
		sale = app.start_sale(100)
		self.pay(sale, 100, 75, "paid-just-in-time")
		app.SALES._sales[sale["id"]]["expires_at"] = int(time.time()) - 1

		app._watch_one_pass(int(time.time()))
		stored = app.SALES.get(sale["id"])
		self.assertEqual(stored["state"], "settled")
		self.assertEqual(stored["credited_native"], 100)

	def test_an_unpaid_sale_past_its_deadline_expires(self):
		sale = app.start_sale(100)
		app.SALES._sales[sale["id"]]["expires_at"] = int(time.time()) - 1
		app._watch_one_pass(int(time.time()))
		self.assertEqual(app.SALES.get(sale["id"])["state"], "expired")

	def test_a_sale_is_not_expired_on_a_failed_read(self):
		"""A provider that will not answer is not evidence of non-payment."""
		sale = app.start_sale(100)
		app.SALES._sales[sale["id"]]["expires_at"] = int(time.time()) - 1

		def refuse(*_args, **_kwargs):
			raise RailProviderError("endpoint", "the indexer is down")

		app.RAIL.observe = refuse
		# Well past the grace period: without the conflict check this would be
		# reviewed; contention is not an answer, so it stays open.
		app._watch_one_pass(int(time.time()) + app.MATURATION_GRACE_SECONDS + 1)
		self.assertEqual(app.SALES.get(sale["id"])["state"], "pending")


class AllocationIsDurable(Harness):
	def test_unreadable_allocator_state_stops_the_shop(self):
		"""Guessing zero re-issues addresses that may already be live."""
		app.start_sale(100)
		app.STATE_FILE.write_text('{"next_index":')
		with self.assertRaises(SystemExit):
			app.Recipients(app.RAIL, app.DEMO_XPUB, "")

	def test_absent_state_is_a_genuine_first_run(self):
		self.assertEqual(app.Recipients(app.RAIL, app.DEMO_XPUB, "")._read_counter(), 0)

	def test_two_allocators_do_not_hand_out_the_same_index(self):
		"""A threading.Lock reaches one interpreter; the counter is shared."""
		one = app.Recipients(app.RAIL, app.DEMO_XPUB, "")
		two = app.Recipients(app.RAIL, app.DEMO_XPUB, "")
		self.assertNotEqual(one.allocate("a")[0], two.allocate("b")[0])

	def test_a_refused_amount_does_not_burn_an_index(self):
		"""Otherwise an unauthenticated caller walks the wallet past its gap
		limit by posting amounts that were never going to be accepted."""
		before = app.RECIPIENTS._read_counter()
		with self.assertRaises(ValueError):
			app.start_sale(0)
		self.assertEqual(app.RECIPIENTS._read_counter(), before)


class SharedAddressIsSingleUse(Harness):
	def _static(self):
		class Static:
			key = "bitcoin:testnet4/native:btc"

			class network:
				namespace, is_testnet = "bitcoin", True

		return app.Recipients(Static, None, "mem1static")

	def test_a_failed_first_sale_releases_the_reservation(self):
		"""Reserving under the lock must not mean one outage disables the shop
		for good: a reservation for a sale that was never created was shown to
		nobody, so it is rolled back."""
		app.RECIPIENTS = self._static()

		def refuse(*_a, **_k):
			raise RailProviderError("endpoint", "the indexer is down")

		original, app.RAIL.capture_baseline = app.RAIL.capture_baseline, refuse
		try:
			with self.assertRaises(RailProviderError):
				app.start_sale(100)
		finally:
			app.RAIL.capture_baseline = original
		self.assertIsNotNone(app.start_sale(100))          # the shop still works

	def test_the_single_use_survives_a_restart(self):
		"""'One sale ever' that forgets on restart is 'one sale per process',
		and the second process hands the address to a second customer while
		the first one's QR is still payable."""
		app.RECIPIENTS = self._static()
		app.start_sale(100)
		revived = self._static()
		with self.assertRaises(ValueError):
			revived.allocate("after-the-restart")

	def test_a_shared_address_backs_one_sale_ever_not_one_at_a_time(self):
		"""The first sale's QR stays payable after that sale ends, so the
		second sale at the same address settles on the first customer."""
		app.RECIPIENTS = self._static()
		first = app.start_sale(100)
		self.pay(first, 100, 70, "first-paid")
		self.assertEqual(self.poll(first).state, "settled")
		with self.assertRaises(ValueError):
			app.start_sale(100)


class TheWatcherIsSupervised(Harness):
	def test_a_failure_outside_the_poll_still_stops_the_shop(self):
		"""Health used to be set only around poll_once, so a fault in listing
		sales killed the thread while the server kept selling."""
		def explode():
			raise RuntimeError("the sales table is gone")

		app.SALES.open_sales = explode
		with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError):
			app.watcher()
		self.assertFalse(app.HEALTH["watching"])
		with self.assertRaises(app.Unhealthy):
			app.start_sale(100)


class OneTransactionCanPayTwoSales(Harness):
	def test_a_batched_payout_settles_both_sales(self):
		"""An exchange withdrawing to several addresses sends ONE transaction.

		Claiming the bare transaction id settled the first sale and left the
		second -- whose customer really paid -- pending forever.
		"""
		first = app.start_sale(100)
		second = app.start_sale(250)
		self.pay(first, 100, 71, "one-batched-payout")
		self.pay(second, 250, 71, "one-batched-payout")

		one, two = self.poll(first), self.poll(second)
		self.assertEqual((one.state, one.credited_native), ("settled", 100))
		self.assertEqual((two.state, two.credited_native), ("settled", 250))

	def test_the_same_output_cannot_pay_one_sale_twice(self):
		"""Scoping by recipient must not weaken the replay guard."""
		sale = app.start_sale(100)
		self.pay(sale, 100, 71, "paid-once")
		self.assertEqual(self.poll(sale).state, "settled")
		self.assertEqual(app.SALES.claimed_at(sale["recipient"]), frozenset({"paid-once"}))


class TheDeadlineIsNotAVerdictOnMoney(Harness):
	def test_a_part_payment_at_the_deadline_goes_to_a_person(self):
		"""`pending` covers a confirmed part payment and money still maturing.
		Expiring on the bare state recorded 50 units confirmed on the chain as
		'the payment window closed with nothing received', sighted zero."""
		sale = app.start_sale(100)
		self.pay(sale, 50, 71, "half-of-it")
		now = int(time.time())
		app.SALES._sales[sale["id"]]["expires_at"] = now - 1

		app._watch_one_pass(now)                       # inside the grace period
		self.assertEqual(app.SALES.get(sale["id"])["state"], "pending")

		app._watch_one_pass(now + app.MATURATION_GRACE_SECONDS + 1)
		stored = app.SALES.get(sale["id"])
		self.assertEqual(stored["state"], "needs-review")
		self.assertEqual(stored["sighted_native"], 50)
		# The reviewer needs to know how much of it was actually creditable.
		self.assertEqual(stored["credited_native"], 50)

	def test_a_claim_conflict_at_the_deadline_does_not_expire_the_sale(self):
		"""A conflict is contention, not an answer; the next poll recomputes.

		The decision must SETTLE and then lose the write, which is the real
		interleaving: this worker read the claimed set before another worker
		committed the same transaction.
		"""
		sale = app.start_sale(100)
		self.pay(sale, 100, 71, "contested")
		app.SALES._credited.add((app.RAIL.key, sale["recipient"], "contested"))  # another worker got it
		app.SALES.claimed_at = lambda _r: frozenset()  # ...after we read the set
		app.SALES._sales[sale["id"]]["expires_at"] = int(time.time()) - 1

		# Well past the grace period: without the conflict check this would be
		# reviewed; contention is not an answer, so it stays open.
		app._watch_one_pass(int(time.time()) + app.MATURATION_GRACE_SECONDS + 1)
		self.assertEqual(app.SALES.get(sale["id"])["state"], "pending")


class KeysAndStateAreChecked(Harness):
	def test_a_master_key_is_refused(self):
		"""Deriving 0/index from a master key gives addresses at a path no
		ordinary wallet scans, so the money arrives where nobody looks."""
		master = ("xpub661MyMwAqRbcFtXgS5sYJABqqG9YLmC4Q1Rdap9gSE8NqtwybGhePY2gZ29ESFjqJ"
		          "oCu1Rupje8YtGqsefD265TMg7usUDFdp6W1EGMcet8")
		with self.assertRaises(SystemExit):
			app.Recipients(app.RAIL, master, "")

	def test_a_missing_state_file_is_not_assumed_to_be_a_first_run(self):
		"""A different working directory looks exactly like a new deployment."""
		app.start_sale(100)
		app.STATE_FILE.unlink()
		del os.environ["CRYPTOPOS_INIT"]
		with self.assertRaises(SystemExit):
			app.Recipients(app.RAIL, app.DEMO_XPUB, "")

	def test_coerced_state_is_refused(self):
		"""int("12") succeeds and means something the file did not say."""
		identity = app.Recipients(app.RAIL, app.DEMO_XPUB, "")._identity
		app.STATE_FILE.write_text(json.dumps(
			{"next_index": "12", "static_used": False, "highest_paid": -1,
			 "identity": identity}))
		with self.assertRaises(SystemExit) as refused:
			app.Recipients(app.RAIL, app.DEMO_XPUB, "")
		self.assertIn("non-negative JSON integer", str(refused.exception))

	def test_another_allocators_counters_are_refused(self):
		"""The file records how far ONE allocator has gone. Rotating the key,
		or pointing the same directory at another rail, used to inherit those
		numbers -- and the new account has no history at all, so an old
		`highest_paid` told the guard its unused run was short when it was the
		whole range."""
		app.start_sale(100)                       # writes this allocator's state
		other = ("xpub6ASuArnXKPbfEwhqN6e3mwBcDTgzisQN1wXN9BJcM47sSikHjJf3UFHKkNAWbWM"
		         "iGj7Wf5uMash7SyYq527Hqck2AxYysAA7xmALppuCkwQ")
		with self.assertRaises(SystemExit) as refused:
			app.Recipients(app.RAIL, other, "")
		self.assertIn("belong to allocator", str(refused.exception))

	def test_a_different_rail_does_not_inherit_the_counters(self):
		"""A Bitcoin payment must not reset the gap counter an EVM deployment
		later reads; its address history is empty."""
		app.start_sale(100)

		class OtherRail:
			key = "bitcoin:testnet4/native:btc"

			class network:
				namespace, reference, is_testnet = "bitcoin", "testnet4", True

		with self.assertRaises(SystemExit):
			app.Recipients(OtherRail, app.DEMO_XPUB, "")


class NoSaleIsHandedOutUnwatched(Harness):
	def test_a_watcher_dying_mid_request_does_not_hand_out_a_live_qr(self):
		"""Checking health once, on the way in, let a customer receive a QR
		that nothing was left to watch."""
		original = app.RAIL.capture_baseline

		def die_during(recipient, configuration):
			app.HEALTH.update(watching=False, why="died mid-request")
			return original(recipient, configuration)

		app.RAIL.capture_baseline = die_during
		with self.assertRaises(app.Unhealthy):
			app.start_sale(100)
		self.assertEqual(len(app.SALES.in_review()), 1)


class ArrivalTimeIsNotOptional(Harness):
	def test_a_confirmed_transfer_without_an_arrival_time_is_refused(self):
		"""Treating an unknown arrival as timely is how a payment made after
		the window settles anyway."""
		sale = app.start_sale(100)
		app.CONFIG["tip"] = 100
		app.CONFIG["transfers"].append({
			"id": "no-time", "to": sale["recipient"], "amount": 100,
			"confs": 3, "height": 71})            # deliberately no "at"
		with self.assertRaises(Exception):
			self.poll(sale)


class OneWorkerOwnsASale(Harness):
	def test_a_lease_stops_a_second_worker_touching_the_same_sale(self):
		"""A conditional write is not enough: two workers reading the chain a
		block apart disagree, and then FIRST writer wins rather than best
		evidence -- a `needs-review` from a stale read can beat a `settled`."""
		sale = app.start_sale(100)
		now = int(time.time())
		token = app.SALES.lease(sale["id"], now)
		self.assertIsNotNone(token)
		self.assertIsNone(app.SALES.lease(sale["id"], now))

		# A worker whose observation outlived its lease must not release the
		# lease its successor now holds, nor write through it.
		app.SALES.release(sale["id"], "a-stale-token")
		self.assertIsNone(app.SALES.lease(sale["id"], now))
		self.assertEqual(
			app.SALES.record(sale["id"], SettlementDecision("needs-review", 0, 0), "stale"),
			app.ALREADY_DECIDED)

		# review() and expire() are fenced too, not only record().
		self.assertFalse(app.SALES.review(
			sale["id"], app.PollResult("pending", 0, 1, (), "stale"), "stale"))
		self.assertFalse(app.SALES.expire(sale["id"], sale["expires_at"] + 1, "stale"))
		self.assertEqual(app.SALES.get(sale["id"])["state"], "pending")

		app.SALES.release(sale["id"], token)
		self.assertIsNotNone(app.SALES.lease(sale["id"], now))

	def test_a_leased_sale_is_skipped_by_the_sweep(self):
		sale = app.start_sale(100)
		self.pay(sale, 100, 71, "paid")
		held = app.SALES.lease(sale["id"], int(time.time()))    # someone else owns it

		# It must not even be OBSERVED. Fencing stops a stale write; skipping
		# stops the wasted provider read that would produce one.
		reads = []
		original = app.RAIL.observe
		app.RAIL.observe = lambda *a, **k: (reads.append(1), original(*a, **k))[1]
		app._watch_one_pass(int(time.time()))
		self.assertEqual(reads, [])
		self.assertEqual(app.SALES.get(sale["id"])["state"], "pending")

		app.SALES.release(sale["id"], held)
		app._watch_one_pass(int(time.time()))
		self.assertEqual(app.SALES.get(sale["id"])["state"], "settled")


class RequestsAreCheckedAgainstTheirSale(Harness):
	def test_a_request_for_another_payment_is_refused(self):
		"""This is the string the customer's money follows."""
		original = app.RAIL.create_request

		def wrong(intent):
			request = original(intent)
			return type(request)(request.rail_key, request.uri, "mem1someone-else",
			                     request.amount_native, request.payer_notice)

		app.RAIL.create_request = wrong
		with self.assertRaises(ValueError):
			app.start_sale(100)

	def test_a_uri_that_merely_MENTIONS_the_address_is_refused(self):
		"""Substring containment is not verification: a URI paying an attacker
		while naming the merchant in a note passed, and the QR pays the URI."""
		original = app.RAIL.create_request

		def decoy(intent):
			request = original(intent)
			return type(request)(
				request.rail_key,
				f"memory:mem1attacker?note={intent.recipient}&amount=100",
				request.recipient, request.amount_native, request.payer_notice)

		app.RAIL.create_request = decoy
		with self.assertRaises(ValueError):
			app.start_sale(100)

	def test_a_uri_naming_another_address_is_refused(self):
		"""The three fields beside a URI are metadata, not proof of it: a
		request naming the right recipient while its URI names another address
		passes a tuple comparison and sends the money elsewhere."""
		original = app.RAIL.create_request

		def swap_uri(intent):
			request = original(intent)
			return type(request)(request.rail_key, "memory:mem1attacker?amount=1",
			                     request.recipient, request.amount_native,
			                     request.payer_notice)

		app.RAIL.create_request = swap_uri
		with self.assertRaises(ValueError):
			app.start_sale(100)

	def test_a_namespace_with_no_parser_is_refused_not_guessed(self):
		"""An instruction nobody can read must not be shown to a payer."""
		class Exotic:
			key = "newchain:testnet/native:new"
			asset = app.RAIL.asset

			class network:
				namespace, reference, is_testnet = "newchain", "testnet", True

		with self.assertRaises(ValueError):
			app._check_payment_identity(Exotic, None, "abc", "newchain:abc?amount=1")

	def test_a_uri_with_the_wrong_scheme_is_refused(self):
		"""bitcoin: and ethereum: addresses do not overlap, but nothing stops a
		defective rail emitting the wrong scheme."""
		original = app.RAIL.create_request

		def wrong_scheme(intent):
			request = original(intent)
			return type(request)(request.rail_key,
			                     f"bitcoin:{intent.recipient}?amount=100",
			                     request.recipient, request.amount_native,
			                     request.payer_notice)

		app.RAIL.create_request = wrong_scheme
		with self.assertRaises(ValueError):
			app.start_sale(100)

	def test_an_evm_uri_naming_another_chain_is_refused(self):
		"""The same address exists on every EVM chain, so `@1` on a Sepolia
		sale sends the customer to mainnet."""
		class Sepolia:
			key = "ethereum:sepolia/native:eth"

			class network:
				namespace, reference, is_testnet = "ethereum", "sepolia", True

			class asset:
				namespace, reference = "native", "eth"

		merchant = "0x4B7115aD9623A528f1845eaf85D166dE1E869BFB"
		app._check_payment_identity(Sepolia, None, merchant,
		                            f"ethereum:{merchant}@11155111?value=1")
		with self.assertRaises(ValueError):
			app._check_payment_identity(Sepolia, None, merchant,
			                            f"ethereum:{merchant}@1?value=1")

	def test_an_erc20_uri_calling_a_different_function_is_refused(self):
		"""`"/transfer" not in path` accepts `/transferFrom`. An ERC-681 path
		names the contract call the wallet makes."""
		merchant = "0x4B7115aD9623A528f1845eaf85D166dE1E869BFB"
		for function in ("transferFrom", "transferAnything", ""):
			with self.assertRaises(ValueError):
				app._check_payment_identity(
					self._usdc(), None, merchant,
					f"ethereum:{self._usdc().asset.reference}@80002/{function}"
					f"?address={merchant}&uint256=1")

	def test_a_uri_asking_for_the_wrong_amount_is_refused(self):
		"""The last field that was taken on trust, and it did not need to be:
		every scheme here states the amount and the intent says what it should
		be. An underpayment sits in review; an overpayment is the customer's
		loss, and neither is the sale's to cause."""
		original = app.RAIL.create_request

		def wrong_amount(intent):
			request = original(intent)
			return type(request)(request.rail_key,
			                     f"memory:{intent.recipient}?amount=1",
			                     request.recipient, request.amount_native,
			                     request.payer_notice)

		app.RAIL.create_request = wrong_amount
		with self.assertRaises(ValueError):
			app.start_sale(100)

	def test_a_uri_stating_the_amount_twice_is_refused(self):
		"""Which one the wallet reads is not the host's decision to leave open."""
		class Btc:
			key = "bitcoin:testnet4/native:btc"

			class network:
				namespace, reference, is_testnet = "bitcoin", "testnet4", True

			class asset:
				namespace, reference, decimals = "native", "btc", 8

		class Intent:
			amount_native = 125_000

		address = "tb1qp5wfcq48h6d63wyy9qz0awtpfqwwv4smhppgv3"
		with self.assertRaises(ValueError):
			app._check_payment_identity(
				Btc, Intent, address,
				f"bitcoin:{address}?amount=0.00125000&amount=9.99")
		with self.assertRaises(ValueError):
			app._check_payment_identity(Btc, Intent, address, f"bitcoin:{address}")

	def test_a_bitcoin_uri_amount_is_read_as_a_decimal(self):
		"""BIP-21 states a decimal amount; ERC-681 states an integer. Reading
		one as the other is off by a factor of 10**decimals."""
		class Btc:
			key = "bitcoin:testnet4/native:btc"

			class network:
				namespace, reference, is_testnet = "bitcoin", "testnet4", True

			class asset:
				namespace, reference, decimals = "native", "btc", 8

		class Intent:
			amount_native = 125_000

		address = "tb1qp5wfcq48h6d63wyy9qz0awtpfqwwv4smhppgv3"
		app._check_payment_identity(Btc, Intent, address,
		                            f"bitcoin:{address}?amount=0.00125000")
		with self.assertRaises(ValueError):
			app._check_payment_identity(Btc, Intent, address,
			                            f"bitcoin:{address}?amount=0.00125001")

	def test_an_evm_uri_suggesting_a_fee_is_refused(self):
		"""ERC-681 lets a URI suggest gasPrice and gasLimit, and a wallet that
		honours them will. A one-wei invoice can cost the customer twenty-one
		ETH in fees while the sale settles perfectly."""
		merchant = "0x4B7115aD9623A528f1845eaf85D166dE1E869BFB"

		class Sepolia:
			key = "ethereum:sepolia/native:eth"

			class network:
				namespace, reference, is_testnet = "ethereum", "sepolia", True

			class asset:
				namespace, reference = "native", "eth"

		app._check_payment_identity(Sepolia, None, merchant,
		                            f"ethereum:{merchant}@11155111?value=1")
		with self.assertRaises(ValueError):
			app._check_payment_identity(
				Sepolia, None, merchant,
				f"ethereum:{merchant}@11155111?value=1&gas=21000&gasPrice=1000000000000000")

	def test_a_bitcoin_uri_carrying_bip72_r_is_refused(self):
		"""`r` does not decorate the instruction, it REPLACES it: a capable
		wallet ignores the address and amount and fetches a PaymentRequest
		from that URL, which may name any output and need not be signed."""
		class Btc:
			key = "bitcoin:testnet4/native:btc"

			class network:
				namespace, reference, is_testnet = "bitcoin", "testnet4", True

			class asset:
				namespace, reference = "native", "btc"

		address = "tb1qp5wfcq48h6d63wyy9qz0awtpfqwwv4smhppgv3"
		app._check_payment_identity(Btc, None, address, f"bitcoin:{address}?amount=0.001")
		with self.assertRaises(ValueError):
			app._check_payment_identity(
				Btc, None, address,
				f"bitcoin:{address}?amount=0.001&r=https%3A%2F%2Fattacker.example%2Freq")

	def test_a_native_evm_uri_calling_a_function_is_refused(self):
		"""A native send calls nothing. A URI that pays the merchant AND names
		a function is instructing something the host never checked."""
		merchant = "0x4B7115aD9623A528f1845eaf85D166dE1E869BFB"

		class Sepolia:
			key = "ethereum:sepolia/native:eth"

			class network:
				namespace, reference, is_testnet = "ethereum", "sepolia", True

			class asset:
				namespace, reference = "native", "eth"

		app._check_payment_identity(Sepolia, None, merchant,
		                            f"ethereum:{merchant}@11155111?value=1")
		with self.assertRaises(ValueError):
			app._check_payment_identity(
				Sepolia, None, merchant,
				f"ethereum:{merchant}@11155111/drain?value=1")

	def test_an_erc20_uri_with_a_different_abi_signature_is_refused(self):
		"""An ERC-681 call is identified by its argument TYPES in order.
		`?uint256=1&address=M&bytes32=..` describes a different call, and a
		wallet encodes a different selector for it."""
		merchant = "0x4B7115aD9623A528f1845eaf85D166dE1E869BFB"
		contract = self._usdc().asset.reference
		good = f"ethereum:{contract}@80002/transfer?address={merchant}&uint256=1"
		app._check_payment_identity(self._usdc(), None, merchant, good)
		for bad in (
			f"ethereum:{contract}@80002/transfer?uint256=1&address={merchant}",
			f"ethereum:{contract}@80002/transfer?address={merchant}&uint256=1&bytes32=de",
			f"ethereum:{contract}@80002/transfer?address={merchant}",
		):
			with self.assertRaises(ValueError):
				app._check_payment_identity(self._usdc(), None, merchant, bad)

	def test_an_erc20_uri_with_two_payees_is_refused(self):
		"""Which one the wallet reads is not the host's decision to leave open."""
		merchant = "0x4B7115aD9623A528f1845eaf85D166dE1E869BFB"
		with self.assertRaises(ValueError):
			app._check_payment_identity(
				self._usdc(), None, merchant,
				f"ethereum:{self._usdc().asset.reference}@80002/transfer"
				f"?address={merchant}&address=0xdead&uint256=1")

	def _usdc(self):
		class Usdc:
			key = "polygon:amoy/erc20:0x41e9"

			class network:
				namespace, reference, is_testnet = "polygon", "amoy", True

			class asset:
				namespace, reference = "erc20", "0x41e94eb019c0762f9bfcf9fb1e58725bfb0e7582"

		return Usdc

	def test_an_erc20_uri_calling_another_contract_is_refused(self):
		class Usdc:
			key = "polygon:amoy/erc20:0x41e9"

			class network:
				namespace, reference, is_testnet = "polygon", "amoy", True

			class asset:
				namespace, reference = "erc20", "0x41e94eb019c0762f9bfcf9fb1e58725bfb0e7582"

		merchant = "0x4B7115aD9623A528f1845eaf85D166dE1E869BFB"
		good = (f"ethereum:{Usdc.asset.reference}@80002/transfer"
		        f"?address={merchant}&uint256=1")
		app._check_payment_identity(Usdc, None, merchant, good)
		with self.assertRaises(ValueError):
			app._check_payment_identity(
				Usdc, None, merchant,
				f"ethereum:0xdeadbeef@80002/transfer?address={merchant}&uint256=1")

	def test_a_duplicate_sale_id_is_refused(self):
		sale = app.start_sale(100)
		with self.assertRaises(ValueError):
			app.SALES.create(dict(sale))


class LateWorkersGetTheTruth(Harness):
	def test_the_stored_outcome_keeps_what_was_sighted(self):
		"""Reporting sighted from credited erases the evidence a reviewer needs."""
		sale = app.start_sale(250)
		# A LATE transfer is a terminal review case: real money, at the right
		# address, and not this sale's to take. (An unreadable one is not --
		# that stays pending, because it is the absence of an answer.)
		self.pay(sale, 250, 71, "arrived-late", at=sale["expires_at"] + 1)
		self.assertEqual(self.poll(sale).state, "needs-review")

		late = app._stored_decision(sale["id"])
		self.assertEqual(late.state, "needs-review")
		self.assertEqual(late.credited_native, 0)
		self.assertEqual(late.sighted_native, 250)


if __name__ == "__main__":
	unittest.main()
