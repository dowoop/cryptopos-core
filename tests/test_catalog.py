"""The built-in catalog states breadth without inflating readiness."""

import unittest

from cryptopos_core import rails
from cryptopos_core.catalog import (
	BUILTIN_RAILS,
	dash_testnet,
	minotari_esmeralda,
	monero_stagenet,
	solana_devnet,
	usdc_solana_devnet,
	zcash_testnet,
)
from cryptopos_core.errors import UnsupportedCapability
from cryptopos_core.plugin import (
	ADDRESS_VALIDATION,
	CHARGE_CAPABILITIES,
	OBSERVATION,
	PAYMENT_REQUEST,
	SETTLEMENT,
	PaymentIntent,
	RecipientBaseline,
)
from cryptopos_core.registry import RailRegistry

EVM = "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"
SOL = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
REFERENCE = "Fk9GjsPFVc7fB8kdEqf6bBLLnPFbYK2VoBEHkYqHqQyi"
TARI = "12HVCEeZQhPauMqdDzV4nYZt67FB8fuRrUWFg8RbY7F7D8FyQh8"
DASH = "yVLSEDNiUf9KAPYLn86HLtBaTPzAhDfksR"
ZCASH = "tmFJH9WpMiH4tC3agcY8qt7zUa2Jw3y9RZK"


def intent(rail, recipient, amount, reference="", baseline=None):
	return PaymentIntent("sale-1", rail.key, recipient, amount, 100, 200, reference, baseline)


class CatalogIdentity(unittest.TestCase):
	def test_core_describes_twelve_rails_and_builds_concrete_entries_for_six(self):
		"""The 2.0 boundary, stated as an inequality rather than a count.

		The rail TABLE still carries twelve entries: core knows every rail's
		decimals, address family and URI scheme, and that knowledge did not
		move. What moved is the six adapters that could observe and settle,
		into `cryptopos-rail-bitcoin`, `-evm`, `-ootle` and `-solana`.

		So the six concrete rails left here are exactly the ones with no
		adapter to move into -- a described rail is not debris, it is a rail
		this package can still build a payment request for.
		"""
		self.assertEqual(len(rails.RAILS), 12)
		self.assertEqual(len(BUILTIN_RAILS), 6)
		self.assertEqual(len({rail.key for rail in BUILTIN_RAILS}), 6)
		self.assertTrue(all(rail.network.is_testnet for rail in BUILTIN_RAILS))

	def test_registry_accepts_every_builtin(self):
		registry = RailRegistry()
		loaded = registry.register_builtins()
		self.assertEqual(len(loaded), 6)
		self.assertEqual(set(registry.keys()), {rail.key for rail in BUILTIN_RAILS})

	def test_every_builtin_declares_the_legacy_rail_binding_category(self):
		# The six that stayed, in catalogue order. `btc`, `eth`, `usdc-eth`,
		# `pol`, `usdc-pol` and `xtr` are absent because their adapters left in
		# 2.0 -- their rows are still in `rails.RAILS`, and a rail package
		# declares the category for the adapter that now owns it.
		legacy = (
			"sol",
			"usdc-sol",
			"xmr",
			"xtm",
			"dash",
			"zec",
		)
		self.assertEqual(
			[rail.binding_category for rail in BUILTIN_RAILS],
			[rails.RAILS[key]["binding_category"] for key in legacy],
		)

	def test_no_builtin_can_charge_because_core_alone_drives_nothing(self):
		"""The whole of the 2.0 break, in one assertion.

		Until 2.0 this listed six keys and the list grew as observers were
		extracted. It is now empty, and that is the contract: installing
		`cryptopos-core` on its own gives a host nothing it can take money on.
		A deployment installs the rail packages it wants, and -- per the parent
		project's D31 -- into every process that runs app code, because a rail
		installed in one process is a rail the terminal can sell on and cannot
		watch.

		An entry appearing here would mean an adapter had crept back into core.
		"""
		complete = [rail.key for rail in BUILTIN_RAILS if CHARGE_CAPABILITIES <= rail.capabilities]
		self.assertEqual(complete, [])

	def test_case_sensitive_solana_mint_survives_asset_identity(self):
		self.assertIn("4zMMC9", usdc_solana_devnet.asset.reference)
		self.assertIn(usdc_solana_devnet.asset.reference, usdc_solana_devnet.key)

	def test_request_only_asset_atomic_scales_are_pinned(self):
		self.assertEqual(
			{
				rail.key: rail.asset.decimals
				for rail in (
					solana_devnet,
					usdc_solana_devnet,
					monero_stagenet,
					minotari_esmeralda,
					dash_testnet,
					zcash_testnet,
				)
			},
			{
				"solana:devnet/native:sol": 9,
				"solana:devnet/spl:4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU": 6,
				"monero:stagenet/native:xmr": 12,
				"minotari:esmeralda/native:xtm": 6,
				"dash:testnet/native:dash": 8,
				"zcash:testnet/native:zec": 8,
			},
		)


class RequestAdapters(unittest.TestCase):
	def test_solana_request_requires_and_carries_the_sale_reference(self):
		request = solana_devnet.create_request(intent(solana_devnet, SOL, 1_000_000, REFERENCE))
		self.assertIn(f"reference={REFERENCE}", request.uri)

	def test_non_observing_rails_refuse_observation_instead_of_simulating_it(self):
		for rail in (solana_devnet, dash_testnet, zcash_testnet):
			with self.subTest(rail=rail.key), self.assertRaises(UnsupportedCapability):
				rail.observe(intent(rail, EVM, 1), {})

	def test_minotari_request_is_esmeralda_bound(self):
		request = minotari_esmeralda.create_request(intent(minotari_esmeralda, TARI, 1_500_000))
		self.assertTrue(request.uri.startswith("tari://esmeralda/"))

	def test_unimplemented_payment_schemes_are_explicitly_unavailable(self):
		for rail in (monero_stagenet,):
			with self.subTest(rail=rail.key):
				self.assertNotIn(PAYMENT_REQUEST, rail.capabilities)
				self.assertTrue(rail.readiness({}).reason_for(PAYMENT_REQUEST))
				with self.assertRaises(UnsupportedCapability):
					rail.create_request(intent(rail, TARI, 1_000_000))

	def test_monero_does_not_claim_stagenet_validation_before_it_exists(self):
		self.assertNotIn(ADDRESS_VALIDATION, monero_stagenet.capabilities)
		readiness = monero_stagenet.readiness({})
		self.assertIn("stagenet", readiness.reason_for(ADDRESS_VALIDATION).lower())

	def test_every_partial_rail_explains_why_observation_is_unavailable(self):
		for rail in BUILTIN_RAILS[1:]:
			with self.subTest(rail=rail.key):
				readiness = rail.readiness({})
				self.assertFalse(readiness.chargeable)
				self.assertTrue(readiness.reason_for(OBSERVATION))

	def test_the_earliest_blocked_step_is_reported_first(self):
		"""`unavailable` is ordered, and the order is the order a sale would hit.

		`RequestRail.readiness` inserts at position 0 twice, so a rail that can
		do nothing reports payment-request before address-validation before
		observation before settlement -- the sequence a cashier would meet them
		in. Nothing asserted that until 2026-08-24, when both inserts survived
		mutation to position 1: the list still held the same four reasons, in an
		order that no longer told the operator which wall they hit first.

		Monero is the only built-in missing both, so it is the only rail that
		exercises both inserts.
		"""
		self.assertEqual(
			[capability for capability, _ in monero_stagenet.readiness({}).unavailable],
			[PAYMENT_REQUEST, ADDRESS_VALIDATION, OBSERVATION, SETTLEMENT],
		)


if __name__ == "__main__":
	unittest.main()
