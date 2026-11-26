"""Read-only providers.

Each provider turns one watched address into a small snapshot:

    balance   (atomic units, as a decimal string; may be None when the chain
               cannot reveal it)
    activities (newest first, bounded, each with a stable id)
    fingerprint (for the state file; never used to decide a notification)

The provider never sees the spend key, never builds or broadcasts a
transaction, and never follows a token. Failures are raised as
BlockwatchError with a short reason the bar can display.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Optional

from . import http
from .config import CHAINS, Chain
from .util import (BlockwatchError, ValidationError, bool_value, digit_string,
                   int_value, iso_from_string, iso_utc, plain, require_dict,
                   require_list, text)

MAX_ACTIVITIES = 25
XMR_MAX_TRANSACTIONS = 5000


@dataclass
class Activity:
    id: str
    kind: str  # in | out | self | activity | balance
    amount_raw: Optional[str] = None
    confirmations: Optional[int] = None
    confirmed: bool = True
    timestamp: Optional[str] = None
    note: str = ""


@dataclass
class Snapshot:
    balance_raw: Optional[str]
    activities: list = field(default_factory=list)
    fingerprint: str = ""
    note: str = ""
    balance_label: str = "balance"
    registered: Optional[bool] = None


def fetch(entry):
    chain = CHAINS.get(entry.get("chain"))
    if chain is None:
        raise BlockwatchError("unknown chain in config")
    if chain.provider == "mempool":
        return _fetch_mempool(entry, chain)
    if chain.provider == "blockcypher":
        return _fetch_blockcypher(entry, chain)
    if chain.provider == "blockscout":
        return _fetch_blockscout(entry, chain)
    if chain.provider == "solana":
        return _fetch_solana(entry, chain)
    if chain.provider == "blink":
        return _fetch_blink(entry, chain)
    if chain.provider == "lws":
        if entry.get("mode") == "wallet-rpc":
            return _fetch_xmr_rpc(entry)
        return _fetch_xmr_lws(entry)
    raise BlockwatchError("no provider for this chain")


def _endpoint(entry, chain):
    return (entry.get("endpoint") or chain.endpoint).rstrip("/")


def _utxo_kind(received, spent):
    """Net direction for a UTXO transaction that may also return change."""
    net = received - spent
    if net > 0:
        return "in", net
    if net < 0:
        return "out", -net
    if received or spent:
        return "self", received
    return None, 0


def _xmr_kind(received, sent):
    if received > 0 and sent > 0:
        return "self"
    if received > 0:
        return "in"
    if sent > 0:
        return "out"
    return "activity"


def _fingerprint(*parts):
    digest = hashlib.sha256()
    for part in parts:
        digest.update(str(part).encode("utf-8", "replace"))
        digest.update(b"\0")
    return digest.hexdigest()[:32]


# -- Bitcoin / Litecoin: mempool.space REST (and the litecoinspace fork) -----

def _stat(stats, key):
    return int_value(stats.get(key), key)


def _fetch_mempool(entry, chain):
    address = entry["address"]
    base = _endpoint(entry, chain)
    encoded = http.path_segment(address)
    info = require_dict(http.request_json(f"{base}/api/address/{encoded}", timeout=20), "address response")
    chain_stats = require_dict(info.get("chain_stats"), "chain_stats")
    mempool_stats = require_dict(info.get("mempool_stats"), "mempool_stats")
    funded = _stat(chain_stats, "funded_txo_sum")
    spent = _stat(chain_stats, "spent_txo_sum")
    mem_funded = _stat(mempool_stats, "funded_txo_sum")
    mem_spent = _stat(mempool_stats, "spent_txo_sum")
    balance = funded - spent + mem_funded - mem_spent
    if balance < 0:
        balance = 0

    payload = http.request_json(f"{base}/api/address/{encoded}/txs", max_bytes=16_000_000,
                                timeout=30)
    transactions = require_list(payload, "transaction list", limit=200)
    activities = []
    skipped = 0
    for item in transactions:
        try:
            activity = _mempool_activity(item, address)
        except ValidationError:
            skipped += 1
            continue
        if activity is not None:
            activities.append(activity)

    notes = []
    if mempool_stats.get("tx_count"):
        notes.append("unconfirmed activity")
    if skipped:
        notes.append(f"{skipped} transaction(s) could not be read")
    return Snapshot(
        balance_raw=str(balance),
        activities=activities[:MAX_ACTIVITIES],
        fingerprint=_fingerprint(
            chain_stats.get("tx_count"), mempool_stats.get("tx_count"),
            funded, spent, mem_funded, mem_spent),
        note=", ".join(notes),
    )


def _mempool_activity(item, address):
    tx = require_dict(item, "transaction")
    txid = text(tx.get("txid"), "txid", limit=200)
    status = require_dict(tx.get("status"), "status")
    confirmed = bool_value(status.get("confirmed"), "confirmed")
    timestamp = iso_utc(status.get("block_time")) if confirmed else None
    confirmations = None
    received = 0
    spent = 0
    for output in require_list(tx.get("vout", []), "vout", limit=200_000):
        if not isinstance(output, dict):
            continue
        if output.get("scriptpubkey_address") == address:
            received += int_value(output.get("value"), "output value")
    for source in require_list(tx.get("vin", []), "vin", limit=200_000):
        if not isinstance(source, dict):
            continue
        prevout = source.get("prevout")
        if isinstance(prevout, dict) and prevout.get("scriptpubkey_address") == address:
            spent += int_value(prevout.get("value"), "input value")
    if received == 0 and spent == 0:
        return None
    kind, amount = _utxo_kind(received, spent)
    return Activity(
        id=txid,
        kind=kind,
        amount_raw=str(amount),
        confirmations=confirmations,
        confirmed=confirmed,
        timestamp=timestamp,
    )


# -- Dogecoin: BlockCypher --------------------------------------------------

def _fetch_blockcypher(entry, chain):
    address = entry["address"]
    base = _endpoint(entry, chain)
    url = http.with_query(f"{base}/addrs/{http.path_segment(address)}/full", "limit", "25")
    url = http.with_query(url, "includeConfidence", "false")
    if entry.get("apiKey"):
        url = http.with_query(url, "token", entry["apiKey"])

    document = require_dict(http.request_json(url), "address response")
    balance = int_value(document.get("final_balance"), "balance")
    if document.get("unconfirmed_balance"):
        note = "includes unconfirmed balance"
    else:
        note = ""

    activities = []
    skipped = 0
    for item in require_list(document.get("txs", []), "transactions", limit=200):
        try:
            activity = _blockcypher_activity(item, address)
        except ValidationError:
            skipped += 1
            continue
        if activity is not None:
            activities.append(activity)

    notes = [note] if note else []
    if skipped:
        notes.append(f"{skipped} transaction(s) could not be read")
    return Snapshot(
        balance_raw=str(balance),
        activities=activities[:MAX_ACTIVITIES],
        fingerprint=_fingerprint(document.get("n_tx"), document.get("unconfirmed_n_tx"), balance),
        note=", ".join(notes),
    )


def _blockcypher_activity(item, address):
    tx = require_dict(item, "transaction")
    txid = text(tx.get("hash"), "transaction hash", limit=200)
    confirmations = int_value(tx.get("confirmations", 0), "confirmations")
    confirmed = confirmations > 0 or bool(tx.get("confirmed"))
    timestamp = iso_from_string(tx.get("confirmed") or tx.get("received"))
    received = 0
    spent = 0
    for output in require_list(tx.get("outputs", []), "outputs", limit=5000):
        if not isinstance(output, dict):
            continue
        addresses = output.get("addresses")
        if isinstance(addresses, list) and address in addresses:
            received += int_value(output.get("value"), "output value")
    for source in require_list(tx.get("inputs", []), "inputs", limit=5000):
        if not isinstance(source, dict):
            continue
        addresses = source.get("addresses")
        if isinstance(addresses, list) and address in addresses:
            spent += int_value(source.get("output_value"), "input value")
    if received == 0 and spent == 0:
        return None
    kind, amount = _utxo_kind(received, spent)
    return Activity(
        id=txid,
        kind=kind,
        amount_raw=str(amount),
        confirmations=confirmations if confirmed else 0,
        confirmed=confirmed,
        timestamp=timestamp,
    )


# -- Ethereum (and any Blockscout instance): Blockscout v2 API --------------

def _blockscout_base(entry, chain):
    base = _endpoint(entry, chain)
    if not base.endswith("/api/v2"):
        base += "/api/v2"
    return base


def _blockscout_get(url):
    try:
        return http.request_json(url)
    except http.HttpError as error:
        if error.status == 404:
            return None
        raise


def _fetch_blockscout(entry, chain):
    address = entry["address"]
    base = _blockscout_base(entry, chain)
    encoded = http.path_segment(address)
    info = _blockscout_get(f"{base}/addresses/{encoded}")
    balance = 0
    if info is not None:
        info = require_dict(info, "address response")
        raw_balance = info.get("coin_balance")
        if raw_balance is not None:
            balance = int(digit_string(raw_balance, "balance"))

    payload = _blockscout_get(f"{base}/addresses/{encoded}/transactions")
    items = []
    if payload is not None:
        items = require_list(require_dict(payload, "transaction response").get("items", []),
                             "transactions", limit=200)

    activities = []
    skipped = 0
    for item in items:
        try:
            activity = _blockscout_activity(item, address)
        except ValidationError:
            skipped += 1
            continue
        if activity is not None:
            activities.append(activity)

    note = "native ETH transfers only"
    if skipped:
        note += f"; {skipped} transaction(s) could not be read"
    return Snapshot(
        balance_raw=str(balance),
        activities=activities[:MAX_ACTIVITIES],
        fingerprint=_fingerprint(balance, *[a.id for a in activities[:10]]),
        note=note,
    )


def _blockscout_activity(item, address):
    tx = require_dict(item, "transaction")
    txid = text(tx.get("hash"), "transaction hash", limit=200)
    target = address.lower()
    sender = tx.get("from")
    sender_hash = sender.get("hash") if isinstance(sender, dict) else None
    recipient = tx.get("to")
    recipient_hash = recipient.get("hash") if isinstance(recipient, dict) else None
    from_here = str(sender_hash or "").lower() == target
    to_here = str(recipient_hash or "").lower() == target
    if not from_here and not to_here:
        return None
    value = tx.get("value")
    amount = int(digit_string(value, "transaction value")) if value is not None else 0
    if from_here and to_here:
        kind = "self"
    elif to_here:
        kind = "in"
    elif from_here:
        kind = "out"
    else:
        kind = "activity"
    status = tx.get("status")
    note = "failed" if status and status != "ok" else ""
    return Activity(
        id=txid,
        kind=kind,
        amount_raw=str(amount),
        confirmed=tx.get("block_number") is not None,
        timestamp=iso_from_string(tx.get("timestamp")),
        note=note,
    )


# -- Solana: JSON-RPC -------------------------------------------------------

def _solana_rpc(entry, method, params):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    headers = {}
    if entry.get("apiKey"):
        headers["Authorization"] = "Bearer " + entry["apiKey"]
    document = require_dict(
        http.request_json(entry.get("endpoint") or CHAINS["sol"].endpoint,
                          method="POST", payload=payload, headers=headers),
        "RPC response")
    if document.get("error"):
        error = document["error"]
        message = error.get("message") if isinstance(error, dict) else None
        raise BlockwatchError(text(message, "RPC error", limit=200))
    return document.get("result")


def _fetch_solana(entry, chain):
    address = entry["address"]
    balance_doc = _solana_rpc(entry, "getBalance", [address])
    balance_doc = require_dict(balance_doc, "balance")
    balance = int_value(balance_doc.get("value"), "balance")

    signatures = _solana_rpc(entry, "getSignaturesForAddress",
                             [address, {"limit": MAX_ACTIVITIES}])
    activities = []
    for item in require_list(signatures, "signatures", limit=100):
        if not isinstance(item, dict):
            continue
        signature = item.get("signature")
        if not isinstance(signature, str) or not 32 <= len(signature) <= 128:
            continue
        if any(ch < " " or ch == "\x7f" for ch in signature):
            continue
        failed = item.get("err") is not None
        status = str(item.get("confirmationStatus") or "")
        activities.append(Activity(
            id=signature,
            kind="activity",
            amount_raw=None,
            confirmed=status in ("confirmed", "finalized") and not failed,
            timestamp=iso_utc(item.get("blockTime")),
            note="failed" if failed else "",
        ))
    note = "balance delta is shown for new activity"
    return Snapshot(
        balance_raw=str(balance),
        activities=activities[:MAX_ACTIVITIES],
        fingerprint=_fingerprint(balance, *[a.id for a in activities[:10]]),
        note=note,
    )


# -- Lightning: Blink (read-only account API) ------------------------------

# One query returns the account's wallets, the BTC wallet balance and its
# newest transactions. Blink serialises amounts as signed minor units (sats for
# a BTC wallet), which is exactly what Blockwatch stores, so nothing is scaled
# here. Only the BTC wallet is read; a USD wallet is ignored.
BLINK_QUERY = """
query blockwatch($first: Int!) {
  me {
    username
    defaultAccount {
      wallets {
        __typename
        ... on BTCWallet {
          balance
          walletCurrency
          transactions(first: $first) {
            edges {
              node {
                id
                status
                direction
                settlementAmount
                createdAt
                initiationVia { __typename }
              }
            }
          }
        }
      }
    }
  }
}
"""

BLINK_INITIATION_NOTES = {
    "InitiationViaLn": "lightning",
    "InitiationViaOnChain": "on-chain",
    "InitiationViaIntraLedger": "blink transfer",
}


def _blink_integer(value, what):
    """A signed minor-unit amount or epoch, as an int (the API may quote it)."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValidationError(f"{what} is not an integer")
    rendered = str(value).strip()
    sign = 1
    if rendered.startswith("-"):
        sign, rendered = -1, rendered[1:]
    if not rendered or len(rendered) > 80 or not rendered.isdigit():
        raise ValidationError(f"{what} is not an integer")
    return sign * int(rendered)


def _blink_id(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValidationError("transaction id is not text")
    return text(str(value), "transaction id", limit=200)


def _blink_request(entry, variables):
    endpoint = (entry.get("endpoint") or CHAINS["lightning"].endpoint).rstrip("/")
    headers = {"X-API-KEY": entry["apiKey"]}
    document = require_dict(
        http.request_json(endpoint, method="POST", headers=headers, timeout=20,
                          payload={"query": BLINK_QUERY, "variables": variables}),
        "Blink response")
    errors = document.get("errors")
    if isinstance(errors, list) and errors:
        first = errors[0]
        message = first.get("message") if isinstance(first, dict) else None
        raise BlockwatchError(plain(message, 200) or "Blink rejected the request")
    return require_dict(document.get("data"), "Blink data")


def _blink_activity(node):
    node = require_dict(node, "transaction")
    if text(node.get("status"), "status", limit=20) != "SUCCESS":
        return None
    direction = text(node.get("direction"), "direction", limit=20)
    if direction == "RECEIVE":
        kind = "in"
    elif direction == "SEND":
        kind = "out"
    else:
        kind = "activity"
    amount = abs(_blink_integer(node.get("settlementAmount"), "settlement amount"))
    initiation = node.get("initiationVia")
    type_name = initiation.get("__typename") if isinstance(initiation, dict) else ""
    return Activity(
        id=_blink_id(node.get("id")),
        kind=kind,
        amount_raw=str(amount),
        confirmed=True,
        timestamp=iso_utc(node.get("createdAt")),
        note=BLINK_INITIATION_NOTES.get(type_name, ""),
    )


def _fetch_blink(entry, chain):
    data = _blink_request(entry, {"first": MAX_ACTIVITIES})
    me = require_dict(data.get("me"), "account")
    username = me.get("username")
    if isinstance(username, str) and username.strip():
        expected = entry["address"].split("@", 1)[0]
        if username.strip().lower() != expected:
            raise BlockwatchError("this Blink API key belongs to a different account")

    account = require_dict(me.get("defaultAccount"), "default account")
    wallets = require_list(account.get("wallets") or [], "wallets", limit=10)
    wallet = next((item for item in wallets
                   if isinstance(item, dict) and item.get("walletCurrency") == "BTC"),
                  None)
    if wallet is None:
        raise BlockwatchError("this Blink account has no BTC wallet")

    balance = max(0, _blink_integer(wallet.get("balance"), "balance"))
    connection = wallet.get("transactions")
    edges = []
    if connection is not None:
        edges = require_list(
            require_dict(connection, "transactions").get("edges") or [],
            "transactions", limit=200)

    activities = []
    pending = 0
    skipped = 0
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        node = edge.get("node")
        if not isinstance(node, dict):
            continue
        if node.get("status") == "PENDING":
            pending += 1
            continue
        try:
            activity = _blink_activity(node)
        except ValidationError:
            skipped += 1
            continue
        if activity is not None:
            activities.append(activity)

    notes = ["Blink BTC wallet; on-chain receipts included"]
    if pending:
        notes.append(f"{pending} pending")
    if skipped:
        notes.append(f"{skipped} transaction(s) could not be read")
    latest = activities[0].id if activities else ""
    return Snapshot(
        balance_raw=str(balance),
        activities=activities[:MAX_ACTIVITIES],
        fingerprint=_fingerprint(balance, latest, len(activities)),
        note=", ".join(notes),
    )


# -- Monero: light wallet server (MyMonero API) -----------------------------

def _lws_post(base, method, payload):
    return http.request_json(f"{base}/{method}", method="POST", payload=payload,
                             max_bytes=16_000_000)


def _register_hint(entry):
    base = entry.get("endpoint") or CHAINS["xmr"].endpoint
    return (f"this wallet is not registered on the light wallet server; register it at "
            f"{base}/register, or use mode 'wallet-rpc'")


def _xmr_pending(entry, note):
    return Snapshot(balance_raw=None, activities=[], fingerprint="pending",
                    note=note, balance_label="received", registered=False)


def _parse_activities(items, parser):
    parsed = []
    skipped = 0
    for item in items:
        try:
            activity = parser(item)
        except ValidationError:
            skipped += 1
            continue
        if activity is not None:
            parsed.append(activity)
    return parsed, skipped


def _lws_request(entry, base, method, payload):
    """Call a light wallet endpoint, registering the wallet once on a 403.

    Returns (document, pending_snapshot): the document on success, or a
    pending snapshot when the server accepted the registration but has not
    finished scanning yet.
    """
    try:
        return _lws_post(base, method, payload), None
    except http.HttpError as error:
        if error.status != 403:
            raise
    try:
        _lws_post(base, "login", dict(payload, create_account=True, generated_locally=True))
    except http.HttpError as login_error:
        if login_error.status in (403, 501):
            raise BlockwatchError(_register_hint(entry)) from None
        raise
    try:
        return _lws_post(base, method, payload), None
    except http.HttpError as error:
        if error.status == 403:
            return None, _xmr_pending(
                entry,
                "waiting for the light wallet server to start scanning; if it stays "
                f"pending, register the wallet at {base}/register")
        raise


def _lws_summary(info):
    info = require_dict(info, "address response")
    received = digit_string(info.get("total_received"), "total received")
    sent = digit_string(info.get("total_sent"), "total sent")
    scanned = int_value(info.get("scanned_block_height"), "scanned height")
    height = int_value(info.get("blockchain_height"), "blockchain height")
    progress = 100 if height <= 0 else min(100, int(scanned * 100 / height))
    notes = []
    if progress < 100:
        notes.append(f"server scanning ({progress}%)")
    notes.append("view key cannot prove outgoing spends")
    return received, sent, progress, notes


def _lws_activities(payload_txs):
    payload_txs = require_dict(payload_txs, "transactions response")
    transactions = require_list(payload_txs.get("transactions", []),
                                "transactions", limit=XMR_MAX_TRANSACTIONS)
    parsed, skipped = _parse_activities(transactions, _xmr_lws_activity)
    # monero-lws returns history in ascending chain order; a mempool entry has
    # no timestamp and is the newest thing in the list.
    parsed.sort(key=lambda activity: (activity.timestamp or "9999", activity.id))
    return list(reversed(parsed[-MAX_ACTIVITIES:])), len(transactions), skipped


def _fetch_xmr_lws(entry):
    base = _endpoint(entry, CHAINS["xmr"])
    payload = {"address": entry["address"], "view_key": entry["viewKey"]}
    info, pending = _lws_request(entry, base, "get_address_info", payload)
    if pending is not None:
        return pending
    received, sent, progress, notes = _lws_summary(info)

    payload_txs, pending = _lws_request(entry, base, "get_address_txs", payload)
    if pending is not None:
        return pending
    activities, total, skipped = _lws_activities(payload_txs)
    if skipped:
        notes.append(f"{skipped} transaction(s) could not be read")

    return Snapshot(
        balance_raw=received,
        activities=activities,
        fingerprint=_fingerprint(received, sent, total),
        note=", ".join(notes),
        balance_label="received",
        registered=True,
    )


def _xmr_lws_activity(item):
    tx = require_dict(item, "transaction")
    txid = text(tx.get("hash"), "transaction hash", limit=200)
    received = int(digit_string(tx.get("total_received", "0"), "total received"))
    sent = int(digit_string(tx.get("total_sent", "0"), "total sent"))
    if received == 0 and sent == 0:
        return None
    mempool = bool(tx.get("mempool"))
    return Activity(
        id=txid,
        kind=_xmr_kind(received, sent),
        amount_raw=str(received if received else sent),
        confirmed=not mempool,
        timestamp=iso_from_string(tx.get("timestamp")),
        note="pending" if mempool else "",
    )


# -- Monero: local monero-wallet-rpc (recommended for correctness) ----------

def _wallet_rpc_url(value):
    value = (value or "").rstrip("/")
    if value.endswith("/json_rpc"):
        return value
    return value + "/json_rpc"


def _wallet_rpc(entry, method, params=None):
    url = _wallet_rpc_url(entry.get("endpoint") or CHAINS["xmr"].endpoint)
    payload = {"jsonrpc": "2.0", "id": "blockwatch", "method": method,
               "params": params or {}}
    auth = None
    if entry.get("authUser"):
        auth = (entry["authUser"], entry.get("authPass") or "")
    document = require_dict(
        http.request_json(url, method="POST", payload=payload, auth=auth, timeout=20),
        "wallet RPC response")
    error = document.get("error")
    if isinstance(error, dict):
        raise BlockwatchError(text(error.get("message"), "wallet RPC error", limit=200))
    return document.get("result")


def _check_rpc_address(entry):
    try:
        wallet_address = _wallet_rpc(entry, "get_address")
    except BlockwatchError:
        # An older or restricted RPC may not answer get_address; the balance
        # and transfers below are the operation that matters.
        return
    if isinstance(wallet_address, dict) and wallet_address.get("address"):
        if wallet_address["address"] != entry["address"]:
            raise BlockwatchError("the wallet RPC holds a different Monero address")


def _rpc_activity(bucket, item):
    if not isinstance(item, dict):
        return None
    try:
        txid = text(item.get("txid"), "transaction hash", limit=200)
    except ValidationError:
        return None
    amount = int(digit_string(item.get("amount"), "transfer amount"))
    try:
        height = int(item.get("height") or 0)
    except (TypeError, ValueError):
        height = 0
    timestamp = iso_utc(item.get("timestamp")) if item.get("timestamp") else None
    pending = bucket in ("pool", "pending")
    confirmations = None
    if item.get("confirmations") is not None:
        confirmations = int_value(item.get("confirmations", 0), "confirmations")
    return Activity(
        id=txid,
        kind=bucket if bucket in ("in", "out") else "activity",
        amount_raw=str(amount),
        confirmations=confirmations,
        confirmed=not pending and height > 0,
        timestamp=timestamp,
        note="pending" if pending else "",
    )


def _rpc_activities(transfers):
    activities = []
    for bucket in ("pool", "pending", "in", "out"):
        items = transfers.get(bucket)
        if not isinstance(items, list):
            continue
        for item in items[:200]:
            activity = _rpc_activity(bucket, item)
            if activity is not None:
                activities.append(activity)
    # Pool/pending can later repeat as in/out; keep the newest stable view.
    activities.sort(key=lambda activity: (activity.timestamp or "", activity.id))
    return list(reversed(activities[-MAX_ACTIVITIES:]))


def _fetch_xmr_rpc(entry):
    _check_rpc_address(entry)

    result = require_dict(_wallet_rpc(entry, "get_balance", {"account_index": 0}), "balance")
    balance = int_value(result.get("balance"), "balance")

    transfers = require_dict(
        _wallet_rpc(entry, "get_transfers", {
            "in": True, "out": True, "pending": True, "pool": True,
            "account_index": 0, "failed": False,
        }),
        "transfers")
    activities = _rpc_activities(transfers)
    return Snapshot(
        balance_raw=str(balance),
        activities=activities,
        fingerprint=_fingerprint(balance, *[a.id for a in activities[:10]]),
        note="view-only balance can include funds already spent",
    )
