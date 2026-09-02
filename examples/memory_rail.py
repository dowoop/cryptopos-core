"""A PaymentRail with a scripted chain: the test double a host needs."""
from cryptopos_core.plugin import (
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
		if not configuration.get("endpoint"):
			return Readiness(self.key, frozenset(), (("payment-request", "no endpoint configured"),
			                                          ("address-validation", "no endpoint configured"),
			                                          ("observation", "no endpoint configured"),
			                                          ("settlement", "no endpoint configured")))
		return Readiness(self.key, self.capabilities)

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
		transfers = tuple(
			TransferObservation(t["id"], t["amount"], True, t["confs"], t["height"])
			for t in configuration.get("transfers", ())
			if t["to"] == intent.recipient and after < t["height"] <= through
		)
		page = ObservationBatch(self.key, intent.intent_id, intent.recipient, "memory",
		                        intent.baseline.tip, tip, after, through, transfers)
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
