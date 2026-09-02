"""The three ways this example lost money before, each now a test.

	python3 -m unittest discover -s examples -t examples

Every one of these failed against an earlier version of `checkout_server.py`.
They are here because an example that teaches an integration pattern is code
that other people run, and a defect in it is a defect in every host that
copied it.
"""

import contextlib
import io
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
		app.RECIPIENTS = app.Recipients(app.RAIL, app.DEMO_XPUB, "")

	def pay(self, sale, amount, height, txid):
		app.CONFIG["tip"] = max(app.CONFIG["tip"], height + 5)
		app.CONFIG["transfers"].append({
			"id": txid, "to": sale["recipient"], "amount": amount,
			"confs": 3, "height": height,
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

		one, two = app.poll_once(first), app.poll_once(second)
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
		self.assertEqual(app.poll_once(first).state, "settled")

		second = app.start_sale(100)
		self.assertNotEqual(second["recipient"], first["recipient"])
		self.assertGreater(second["index"], first["index"])

		# The first customer pays the old QR again. It must reach nothing.
		self.pay(first, 100, 100, "first-paid-again")
		self.assertEqual(app.poll_once(second).state, "pending")

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
			baseline=app.RAIL.capture_baseline(first["recipient"], {"tip": 60}))
		second["recipient"] = first["recipient"]
		self.pay(first, 100, 75, "tx-contested")

		barrier = threading.Barrier(2)
		real = app.SALES.claimed

		def claimed_then_wait():
			seen = real()
			barrier.wait()
			return seen

		app.SALES.claimed = claimed_then_wait
		out = {}
		threads = [threading.Thread(target=lambda s=s: out.__setitem__(s["id"], app.poll_once(s)))
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
		self.assertEqual(app.poll_once(sale).state, "settled")

		late = SettlementDecision("needs-review", 0, 100, reason="a slower worker")
		self.assertEqual(app.SALES.record(sale["id"], late), app.ALREADY_DECIDED)
		self.assertEqual(app.SALES.get(sale["id"])["state"], "settled")
		self.assertEqual(app.SALES.get(sale["id"])["credited_native"], 100)


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
		self.assertTrue(app.SALES.expire(sale["id"], sale["expires_at"] + 1))
		self.assertEqual(app.SALES.get(sale["id"])["state"], "expired")
		self.assertEqual(app.SALES.open_sales(), [])

	def test_a_live_sale_does_not_expire_early(self):
		sale = app.start_sale(100)
		self.assertFalse(app.SALES.expire(sale["id"], sale["expires_at"] - 1))
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
		app._watch_one_pass(int(time.time()))
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
		self.assertEqual(app.poll_once(first).state, "settled")
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


class LateWorkersGetTheTruth(Harness):
	def test_the_stored_outcome_keeps_what_was_sighted(self):
		"""Reporting sighted from credited erases the evidence a reviewer needs."""
		sale = app.start_sale(250)
		app.CONFIG["tip"] = 100
		app.CONFIG["transfers"].append({
			"id": "tx-?", "to": sale["recipient"], "amount": 250,
			"confs": 3, "height": 71, "unreadable": True})
		self.assertEqual(app.poll_once(sale).state, "needs-review")

		late = app._stored_decision(sale["id"])
		self.assertEqual(late.state, "needs-review")
		self.assertEqual(late.credited_native, 0)
		self.assertEqual(late.sighted_native, 250)


if __name__ == "__main__":
	unittest.main()
