"""A payment rail with a scripted chain, for testing a host without one.

`MemoryRail` implements the full `PaymentRail` protocol against a dictionary
instead of a provider, so a host's own bugs -- the double credit, the partial
read, the late baseline, the sale that settles on another sale's money -- are
all reachable with no network, no funds, and no waiting.

	from cryptopos_core.testing import MemoryRail

	rail = MemoryRail()
	chain = {"endpoint": "memory://", "tip": 60, "page": 20, "transfers": []}

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

from .plugin import (
	ADDRESS_VALIDATION, OBSERVATION, PAYMENT_REQUEST, SETTLEMENT,
	Asset, Network, ObservationBatch, PaymentRequest, Readiness,
	RecipientBaseline, SettlementDecision, TransferObservation,
	NOT_UNCONDITIONAL,
)


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
		return RecipientBaseline(self.key, recipient, "memory", tip=configuration["tip"])

	def create_request(self, intent):
		return PaymentRequest(self.key, f"memory:{intent.recipient}?amount={intent.amount_native}",
		                      intent.recipient, intent.amount_native)

	def observe(self, intent, configuration, previous=None):
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
		transfers = tuple(
			TransferObservation(t["id"], t["amount"], True, t["confs"], t["height"])
			if not t.get("unreadable") else
			TransferObservation(t["id"], t["amount"], False, 0)
			for t in visible
		)
		unresolved = tuple(t["id"] for t in visible if t.get("unreadable"))
		page = ObservationBatch(self.key, intent.intent_id, intent.recipient, "memory",
		                        intent.baseline.tip, tip, after, through, transfers,
		                        0, (), None, unresolved)
		return previous.extend(page) if previous else page

	def settle(self, intent, observations, claimed_transaction_ids=frozenset()):
		observations.require_intent(intent)
		sighted = sum(t.amount_native for t in observations.transfers)
		usable = [t for t in observations.transfers
		          if t.confirmations >= 1 and t.transaction_id not in claimed_transaction_ids]
		credited = sum(t.amount_native for t in usable)
		if observations.unresolved_transaction_ids:
			return SettlementDecision("needs-review", 0, sighted, reason="a transaction could not be read")
		if credited >= intent.amount_native:
			return SettlementDecision("settled", credited, sighted,
			                          tuple(t.transaction_id for t in usable))
		return SettlementDecision("pending", 0, sighted,
		                          reason=f"{credited} of {intent.amount_native} seen")
