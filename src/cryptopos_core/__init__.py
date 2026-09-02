"""cryptopos-core — installable payment rails that owe nothing to a framework.

The public surfaces separate mechanism from host policy:

    plugin   concrete network/asset identities and immutable rail values
    registry explicit built-in and installed entry-point discovery
    catalog  the described rails, including honest partial capability reports

    rates    quoting an asset in microcents, under stricter rules when the
             money is real; converting cents to exact native units
    addresses checksum-verifying a receiving address and binding it to the
             sale's network -- the last check before money moves
    rails    what the terminal knows about each chain, as one table of pure
             data, plus the integer unit math that reads it
    uri      a payment URI per scheme — the exact string the QR encodes
    qr       a payment URI as a module grid, ready to draw
    errors   documented refusals at pricing, provider, and payment boundaries

What is NOT here is deliberate: persistence, scheduling, permissions, and the
sale's state machine. Those are where a host framework is genuinely better
than a library, and a POS that hid them inside a package would be fighting
whatever it was embedded in.

Core drives no rails of its own. `register_builtins()` registers the six
request-only catalogue entries; every drivable rail is a separately installed
package found through the `cryptopos.rails` entry-point group:

    >>> from cryptopos_core.registry import RailRegistry
    >>> registry = RailRegistry()
    >>> len(registry.register_builtins())
    6
    >>> registry.discover()          # doctest: +SKIP
    (<BitcoinTestnet4 ...>, ...)

The chain reader moved to `cryptopos-rail-ootle` in 2.0 and is imported from
`cryptopos_rail_ootle.chain`. README.md is the cookbook; `tools/readme.py`
checks every example in it against the built wheel.
"""

from . import (
	addresses,
	catalog,
	conformance,
	errors,
	modes,
	plugin,
	qr,
	qrcodegen,
	rails,
	rates,
	registry,
	uri,
)
from .errors import (
	AddressRefused,
	AmountNotRepresentable,
	CryptoPosError,
	DuplicateRail,
	FeedsDisagree,
	InvalidAmount,
	InvalidAsset,
	InvalidMode,
	InvalidPaymentIdentity,
	InvalidRailPlugin,
	InvalidRate,
	RailNotInstalled,
	RailPluginError,
	RailProviderError,
	RateUnavailable,
	UnsupportedCapability,
	UnsupportedRail,
)
from .modes import VALID_MODES
from .rails import RAILS, rail_for, rail_keys
from .uri import build_uri

__version__ = "2.1.1"

__all__ = [
	"RAILS",
	"VALID_MODES",
	"AddressRefused",
	"AmountNotRepresentable",
	"CryptoPosError",
	"DuplicateRail",
	"FeedsDisagree",
	"InvalidAmount",
	"InvalidAsset",
	"InvalidMode",
	"InvalidPaymentIdentity",
	"InvalidRailPlugin",
	"InvalidRate",
	"RailNotInstalled",
	"RailPluginError",
	"RailProviderError",
	"RateUnavailable",
	"UnsupportedCapability",
	"UnsupportedRail",
	"__version__",
	"addresses",
	"build_uri",
	"catalog",
	"conformance",
	"errors",
	"modes",
	"plugin",
	"qr",
	"qrcodegen",
	"rail_for",
	"rail_keys",
	"rails",
	"rates",
	"registry",
	"uri",
]
