#!/usr/bin/env python3
"""A complete crypto checkout in one stdlib file.

	python3 checkout_server.py                 # scripted rail, no chain, no funds
	CRYPTOPOS_RAIL=bitcoin:testnet4/native:btc \
	CRYPTOPOS_ENDPOINT=https://mempool.space/testnet4/api \
	CRYPTOPOS_RECIPIENT=tb1q... python3 checkout_server.py

Nothing here is Flask-, Django-, or FastAPI-specific: the only framework
contact points are "read a request" and "write a response". Everything between
them is the five-call rail protocol, and it is the same five calls in any host.

The four things a host must get right are marked (1)-(4) below. They are the
four this project has actually got wrong, with real money, at least once.
"""

import json
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from cryptopos_core.conformance import require_conformant
from cryptopos_core.plugin import PaymentIntent
from cryptopos_core.qr import modules_for
from cryptopos_core.registry import RailRegistry

WINDOW_SECONDS = 15 * 60
POLL_SECONDS = 5


# --------------------------------------------------------------------------
# Storage. This is the part you replace with your database.
# --------------------------------------------------------------------------
class Sales:
	"""Sales, plus the set of transaction ids already spent on one.

	(1) THE CREDITED IDS AND THE SALE STATE MUST MOVE TOGETHER. `settle` is
	pure -- it will happily credit the same transfer to a second sale unless
	you tell it what is already spent. Persist `decision.transaction_ids` in
	the SAME transaction that writes the settled state, or a crash between the
	two writes pays an invoice twice.
	"""

	def __init__(self):
		self._lock = threading.Lock()
		self._sales = {}
		self._credited = set()

	def create(self, sale):
		with self._lock:
			self._sales[sale["id"]] = sale
		return sale

	def get(self, sale_id):
		with self._lock:
			return dict(self._sales[sale_id]) if sale_id in self._sales else None

	def open_sales(self):
		with self._lock:
			return [dict(s) for s in self._sales.values() if s["state"] == "pending"]

	def claimed(self):
		with self._lock:
			return frozenset(self._credited)

	def record(self, sale_id, decision):
		with self._lock:
			sale = self._sales[sale_id]
			sale["state"] = decision.state
			sale["reason"] = decision.reason
			sale["credited_native"] = decision.credited_native
			sale["transaction_ids"] = list(decision.transaction_ids)
			self._credited.update(decision.transaction_ids)   # atomic with the line above


SALES = Sales()


# --------------------------------------------------------------------------
# The rail. One registry, built once, at start-up.
# --------------------------------------------------------------------------
def load_rail():
	registry = RailRegistry()
	registry.discover()                      # every installed cryptopos-rail-* package
	key = os.environ.get("CRYPTOPOS_RAIL")
	if not key:
		from memory_rail import MemoryRail   # the demo default; see recipe 9
		registry.register(MemoryRail())
		key = MemoryRail.key
	rail = registry.get(key)                 # raises RailNotInstalled, which is the honest answer
	config = {"endpoint": os.environ.get("CRYPTOPOS_ENDPOINT", "memory://")}
	config.update(DEMO_CHAIN if key.startswith("memory:") else {})
	require_conformant(rail, config)         # capability claims must match readiness
	readiness = rail.readiness(config)
	if not readiness.chargeable:
		raise SystemExit(f"{key} cannot be charged here: {readiness.unavailable}")
	return rail, config


DEMO_CHAIN = {"tip": 60, "page": 20, "transfers": []}
RAIL, CONFIG = None, None
RECIPIENT = os.environ.get("CRYPTOPOS_RECIPIENT", "mem1alice")


def start_sale(amount_native):
	verdict, why = RAIL.validate_recipient(RECIPIENT)
	if verdict == "refused":
		raise ValueError(f"receiving address refused: {why}")

	# (2) CAPTURE THE BASELINE BEFORE THE PAYER SEES ANYTHING. It records the
	# chain position the sale starts from. Capture it late and a transfer that
	# arrived before this sale existed can be credited to it.
	baseline = RAIL.capture_baseline(RECIPIENT, CONFIG)
	now = int(time.time())
	intent = PaymentIntent(
		intent_id=f"sale-{uuid.uuid4().hex[:12]}",
		rail_key=RAIL.key,
		recipient=RECIPIENT,
		amount_native=amount_native,
		created_at_epoch=now,
		expires_at_epoch=now + WINDOW_SECONDS,
		baseline=baseline,
	)
	request = RAIL.create_request(intent)
	return SALES.create({
		"id": intent.intent_id, "intent": intent, "uri": request.uri,
		"amount_native": amount_native, "state": "pending", "reason": "",
		"credited_native": 0, "transaction_ids": [], "expires_at": intent.expires_at_epoch,
	})


def poll_once(sale):
	"""One full observation cycle for one sale, then a settlement decision."""
	intent = sale["intent"]

	# (3) OBSERVE IS BOUNDED. It returns what it could read in one provider
	# call; loop until the batch reports `complete`, then decide. Deciding on
	# a partial read is deciding on a partial payment.
	batch = RAIL.observe(intent, CONFIG)
	while not batch.complete:
		batch = RAIL.observe(intent, CONFIG, batch)

	decision = RAIL.settle(intent, batch, claimed_transaction_ids=SALES.claimed())
	if decision.state != "pending":
		SALES.record(sale["id"], decision)
	return decision


def demo_payer():
	"""DEMO ONLY. Stands in for a customer with a wallet and the real chain.

	With CRYPTOPOS_RAIL set this never runs: a real chain needs a real payer.
	"""
	while True:
		time.sleep(8)
		for sale in SALES.open_sales():
			CONFIG["tip"] += 20                      # the chain moves on
			CONFIG["transfers"].append({
				"id": f"tx-{sale['id'][-6:]}", "to": RECIPIENT,
				"amount": sale["amount_native"], "confs": 3,
				"height": CONFIG["tip"] - 5,
			})


def watcher():
	"""Scheduling is the host's job. In production this is your job queue."""
	while True:
		for sale in SALES.open_sales():
			try:
				poll_once(sale)
			except Exception as exc:                      # a provider hiccup is not a verdict
				print(f"  watch {sale['id']}: {type(exc).__name__}: {exc}")
		time.sleep(POLL_SECONDS)


# --------------------------------------------------------------------------
# HTTP. The only framework-shaped code in the file.
# --------------------------------------------------------------------------
def qr_svg(uri):
	"""(4) THE QR IS A GRID, NOT MARKUP -- draw it wherever you render."""
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
<button>Charge</button></form>"""


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
			return self._send(200, FORM)
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
				amount=sale["amount_native"], symbol=RAIL.asset.symbol,
				qr=qr_svg(sale["uri"]), uri=sale["uri"], id=sale["id"]))
		return self._send(404, "not found")

	def do_POST(self):
		if urlparse(self.path).path != "/sales":
			return self._send(404, "not found")
		length = int(self.headers.get("Content-Length") or 0)
		fields = parse_qs(self.rfile.read(length).decode())
		try:
			sale = start_sale(int(fields.get("amount", ["0"])[0]))
		except Exception as exc:
			return self._send(400, f"refused: {exc}")
		self.send_response(303)
		self.send_header("Location", f"/sales/{sale['id']}")
		self.end_headers()

	def log_message(self, *args):
		pass


def main():
	global RAIL, CONFIG
	RAIL, CONFIG = load_rail()
	print(f"rail {RAIL.key} -> {RECIPIENT}")
	threading.Thread(target=watcher, daemon=True).start()
	if RAIL.key.startswith("memory:"):
		threading.Thread(target=demo_payer, daemon=True).start()
		print("demo rail: a scripted payer settles each sale about 8s after you charge it")
	print("http://127.0.0.1:8099")
	ThreadingHTTPServer(("127.0.0.1", 8099), Handler).serve_forever()


if __name__ == "__main__":
	main()
