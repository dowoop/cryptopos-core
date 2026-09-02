"""A payment rail with a scripted chain, for testing a host without one.

`MemoryRail` implements the full `PaymentRail` protocol against a dictionary
instead of a provider, so a host's own bugs -- the double credit, the partial
read, the late baseline, the sale that settles on another sale's money -- are
all reachable with no network, no funds, and no waiting.

	from cryptopos_core.testing import MemoryRail

	rail = MemoryRail()
	chain = {"endpoint": "memory://", "tip": 60, "page": 20, "transfers": []}

A scripted transfer is a dict with `id`, `to`, `amount`, `confs` and `height`,
plus an optional `at` -- the epoch second of the block carrying it. Supply `at`
to exercise a host's expiry rule: a transfer that landed after the intent's
window is sighted and never creditable.

It ships in the wheel on purpose. A test double that is only in the repository
is a test double the people integrating this library do not have, and testing
the failure paths is the part of a payment integration nobody should have to
reinvent.

This module is for tests. It IS a real `PaymentRail` -- deliberately, because a
double the protocol would reject cannot test a host against the protocol -- but
it is a rail to nowhere: it names a network that does not exist, publishes no
entry point, and is not among the catalogue rails `register_builtins()`
returns. `discover()` cannot find it and no deployment can acquire it by
accident; importing it is an explicit act. It must never appear in a
deployment's registry.
"""

from .errors import InvalidRailPlugin, RailProviderError
from .plugin import (
	ADDRESS_VALIDATION, OBSERVATION, PAYMENT_REQUEST, SETTLEMENT,
	Asset, Network, ObservationBatch, PaymentRequest, Readiness,
	RecipientBaseline, SettlementDecision, TransferObservation,
	NOT_UNCONDITIONAL,
)


def _require_endpoint(configuration):
	if not configuration.get("endpoint"):
		raise RailProviderError("memory", "no endpoint configured")


class MemoryRail:
	key = "memory:testnet/native:tok"
	network = Network("memory", "testnet", True)
	asset = Asset("native", "tok", "TOK", 2)
	capabilities = frozenset({ADDRESS_VALIDATION, PAYMENT_REQUEST, OBSERVATION, SETTLEMENT})
	binding_category = NOT_UNCONDITIONAL

	def readiness(self, configuration):
		"""Report chargeable only when this configuration can actually be driven.

		A test double that says `chargeable=True` and then hangs teaches the
		opposite of what readiness means. Two configurations used to do exactly
		that: `page=0` never advanced the observation window, so the loop every
		recipe prescribes could not terminate, and an absent `tip` raised
		`KeyError` from `capture_baseline` after readiness had already passed.
		Both are now refusals with reasons, which is what a real rail does when
		its provider cannot serve the method it needs.
		"""
		# PER CAPABILITY, and only the ones actually affected. Collecting every
		# problem into one string and applying it to all four said address
		# validation was unavailable "because there is no endpoint configured"
		# -- while `validate_recipient` works perfectly without one. A blanket
		# reason is a plausible sentence that is not true of the capability it
		# is attached to, which is worse than giving no reason.
		reasons = []
		if not configuration.get("endpoint"):
			reasons.append("no endpoint configured")
		tip = configuration.get("tip")
		if not isinstance(tip, int) or isinstance(tip, bool) or tip < 0:
			reasons.append("configuration needs an integer 'tip' -- the scripted chain height")
		page = configuration.get("page", 10**9)
		if not isinstance(page, int) or isinstance(page, bool) or page < 1:
			reasons.append("configuration needs a 'page' of at least 1, or observation cannot advance")
		if not reasons:
			return Readiness(self.key, self.capabilities)
		# ONLY OBSERVATION. Validating an address, building a request and
		# deciding a settlement all read nothing from the provider -- `settle`
		# is a pure function of the intent and a batch you already hold, and
		# reporting it unavailable was an imposed policy dressed as a fact.
		# `chargeable` is still False, because it needs all four.
		blocked = (OBSERVATION,)
		reason = "; ".join(reasons)
		return Readiness(self.key, frozenset(self.capabilities) - set(blocked),
		                 tuple((capability, reason) for capability in blocked))

	def validate_recipient(self, recipient):
		return ("ok", "") if recipient.startswith("mem1") else ("refused", "not a memory address")

	def capture_baseline(self, recipient, configuration):
		_require_endpoint(configuration)
		return RecipientBaseline(self.key, recipient, "memory", tip=configuration["tip"])

	def create_request(self, intent):
		return PaymentRequest(self.key, f"memory:{intent.recipient}?amount={intent.amount_native}",
		                      intent.recipient, intent.amount_native)

	def observe(self, intent, configuration, previous=None):
		# READINESS MUST BE TRUE OF THE THING IT DESCRIBES. This refused
		# observation when `endpoint` was empty and then observed perfectly
		# well without one, which made the endpoint decorative and the reason
		# invented. A rail that says it needs its provider has to need it.
		_require_endpoint(configuration)
		tip = configuration["tip"]
		after = previous.observed_through_tip if previous else intent.baseline.tip
		through = min(after + configuration.get("page", 10**9), tip)
		visible = [t for t in configuration.get("transfers", ())
		           if t["to"] == intent.recipient and after < t["height"] <= through]
		# A transfer marked "unreadable" was seen but its status could not be
		# established -- the provider was asked and did not answer. That is the
		# distinction settlement needs in order to route a sale to review
		# instead of deciding it, so the double must be able to produce it.
		# An unreadable transfer is reported as UNCONFIRMED, not dropped. It was
		# seen; only its status is unknown. Dropping it would hide the money
		# from `sighted_native`, and the gap between sighted and credited is
		# the whole reason a person is asked to look.
		# An UNCONFIRMED transfer is ordinary: money in the mempool, or money
		# still maturing toward a rail's confirmation depth. Building every
		# readable transfer as confirmed made `confs=0` raise out of the
		# protocol's own validation, so the double could not model the most
		# common pending state there is.
		transfers = tuple(
			TransferObservation(t["id"], t["amount"], False, 0)
			if t.get("unreadable") or not t["confs"] else
			TransferObservation(t["id"], t["amount"], True, t["confs"], t["height"],
			                    t.get("at"))
			for t in visible
		)
		unresolved = tuple(t["id"] for t in visible if t.get("unreadable"))
		page = ObservationBatch(self.key, intent.intent_id, intent.recipient, "memory",
		                        intent.baseline.tip, tip, after, through, transfers,
		                        0, (), None, unresolved)
		return previous.extend(page) if previous else page

	#: How deep a transfer must be before this rail will credit it. Overridden
	#: per instance so a test can model a three-confirmation rail.
	min_confirmations = 1

	def settle(self, intent, observations, claimed_transaction_ids=frozenset()):
		observations.require_intent(intent)
		sighted = sum(t.amount_native for t in observations.transfers)
		# A TRANSFER THAT LANDED AFTER THE WINDOW CLOSED IS NOT CREDITABLE.
		# Without a block time the double could not tell a timely payment from
		# a late one, so a sale whose deadline had passed settled anyway and a
		# host's expiry rule described something that never happened.
		# LATENESS IS JUDGED ON TRANSFERS THAT PASSED THE DEPTH GATE. A late
		# transfer one block deep used to return terminal `needs-review` with
		# money sighted, which let a host treat a shallow confirmation as a
		# durable fact. Until it is deep enough, the honest answer is that
		# nothing has been established yet.
		deep = [t for t in observations.transfers if t.confirmations >= max(1, int(self.min_confirmations))]
		late = [t for t in deep
		        if t.block_time_epoch is not None and t.block_time_epoch > intent.expires_at_epoch]
		# UNKNOWN IS NOT TIMELY. A confirmed transfer with no arrival time used
		# to be credited as though it had arrived in the window, so a payment
		# made after expiry settled simply because the script did not say when
		# it landed. Refuse to script one rather than fail open on it.
		for transfer in observations.transfers:
			if transfer.confirmed and transfer.block_time_epoch is None:
				raise InvalidRailPlugin(
					f"scripted transfer {transfer.transaction_id!r} is confirmed but carries no "
					f"'at' -- expiry is judged on when money arrived, and treating an unknown "
					f"arrival as timely is how a late payment settles")
		# A DEPTH GATE, like every real rail has. `min_confirmations` in the
		# configuration lets a host model Sepolia's three or Bitcoin's one, and
		# therefore the state that matters most: money confirmed on the chain
		# and not yet deep enough for the rail to credit it.
		usable = [t for t in deep
		          if t not in late
		          and t.transaction_id not in claimed_transaction_ids]
		credited = sum(t.amount_native for t in usable)
		if observations.unresolved_transaction_ids:
			# NOT A VERDICT, SO NOT TERMINAL. "I could not find out" is the
			# absence of a decision; making it `needs-review` on the first
			# failed read turns one transient provider hiccup into a sale a
			# person has to rescue. It stays pending and is asked again -- and
			# a host whose window has closed can then review it deliberately.
			return SettlementDecision("pending", 0, sighted,
			                          reason="a transaction could not be read yet")
		if late:
			return SettlementDecision("needs-review", 0, sighted,
			                          reason="a transfer arrived after the payment window closed")
		if credited >= intent.amount_native:
			return SettlementDecision("settled", credited, sighted,
			                          tuple(t.transaction_id for t in usable))
		# A PENDING DECISION CAN STILL NAME CREDITABLE MONEY. A part payment is
		# not nothing, and a host that shows the customer how much is
		# outstanding needs the number. `SettlementDecision` allows it; only a
		# SETTLED decision may claim transaction ids.
		return SettlementDecision("pending", credited, sighted,
		                          reason=f"{credited} of {intent.amount_native} seen")
