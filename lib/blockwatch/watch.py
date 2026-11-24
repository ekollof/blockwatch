"""Poll orchestration and change detection.

The first successful check of an address is a baseline: it is stored, but it
does not notify. Every later check compares the provider's recent activity with
the ids already seen, and turns anything new into an event for the bar. A
balance-only change (an internal transfer, or a chain where transaction detail
is not public) is reported as its own event, once.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from .config import CHAINS, chain_list, config_lock, load_config, server_list
from .providers import Activity, fetch
from .rates import fiat_fields, fiat_list, rates_for
from .state import (load_state, prune_state, remember_seen, save_state)
from .util import BlockwatchError, format_units

MAX_EVENTS_PER_ADDRESS = 25
MAX_REPORT_EVENTS = 60


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _delta_event(previous_balance, current_balance):
    if previous_balance is None or current_balance is None:
        return None
    try:
        delta = int(current_balance) - int(previous_balance)
    except (TypeError, ValueError):
        return None
    if delta == 0:
        return None
    kind = "in" if delta > 0 else "out"
    raw = str(abs(delta))
    return Activity(
        id=f"balance:{current_balance}:{delta}",
        kind=kind,
        amount_raw=raw,
        confirmed=True,
        note="net balance change",
    )


def _event_dict(activity, entry, chain, checked_at):
    amount = None
    if activity.amount_raw is not None:
        try:
            amount = format_units(activity.amount_raw, chain.decimals)
        except BlockwatchError:
            amount = None
    return {
        "id": activity.id,
        "kind": activity.kind,
        "amount": amount,
        "amountRaw": activity.amount_raw,
        "symbol": chain.symbol,
        "confirmed": bool(activity.confirmed),
        "confirmations": activity.confirmations,
        "timestamp": activity.timestamp,
        "note": activity.note,
        "addressId": entry["id"],
        "chain": chain.id,
        "label": entry.get("label") or "",
        "checkedAt": checked_at,
    }


def _result(entry, chain, snapshot=None, error=None, initialized=False,
            events=None, truncated=0, previous=None, rates=None):
    balance_raw = snapshot.balance_raw if snapshot else (previous or {}).get("balanceRaw")
    balance = None
    if balance_raw is not None:
        try:
            balance = format_units(balance_raw, chain.decimals)
        except BlockwatchError:
            balance = None
    fiat = fiat_fields(balance_raw, chain.decimals, chain.symbol, rates)
    result = {
        "id": entry["id"],
        "chain": chain.id,
        "name": chain.name,
        "symbol": chain.symbol,
        "label": entry.get("label") or "",
        "address": entry["address"],
        "mode": entry.get("mode"),
        "endpoint": entry.get("endpoint") if chain.id == "xmr" else None,
        "hasViewKey": bool(entry.get("viewKey")) if chain.id == "xmr" else False,
        "balance": balance,
        "balanceRaw": balance_raw,
        "balanceLabel": snapshot.balance_label if snapshot else "balance",
        "fiatValue": fiat["fiatValue"],
        "fiatCurrency": fiat["fiatCurrency"],
        "fiatSymbol": fiat["fiatSymbol"],
        "fiatDecimals": fiat["fiatDecimals"],
        "status": "error" if error else ("pending" if snapshot and snapshot.registered is False else "ok"),
        "error": error,
        "note": snapshot.note if snapshot else "",
        "initialized": bool(initialized),
        "registered": snapshot.registered if snapshot else None,
        "newEvents": events or [],
        "truncated": truncated,
    }
    return result


def _with_balance_direction(fresh, previous_balance, current_balance):
    """Turn a single anonymous activity into a directed net balance move."""
    delta = _delta_event(previous_balance, current_balance)
    if not fresh:
        return [delta] if delta is not None else []
    if len(fresh) == 1 and fresh[0].amount_raw is None and delta is not None:
        activity = fresh[0]
        return [Activity(
            id=activity.id,
            kind=delta.kind,
            amount_raw=delta.amount_raw,
            confirmations=activity.confirmations,
            confirmed=activity.confirmed,
            timestamp=activity.timestamp,
            note="net balance change",
        )]
    return fresh


def _fresh_activities(snapshot, previous, activities):
    if previous is None:
        return [], 0
    seen = set(previous.get("seen", []))
    fresh = [activity for activity in activities if activity.id not in seen]
    if len(fresh) > MAX_EVENTS_PER_ADDRESS:
        truncated = len(fresh) - MAX_EVENTS_PER_ADDRESS
        fresh = fresh[:MAX_EVENTS_PER_ADDRESS]
    else:
        truncated = 0
    return _with_balance_direction(fresh, previous.get("balanceRaw"), snapshot.balance_raw), truncated


def _check(entry, previous, checked_at, rates):
    chain = CHAINS[entry["chain"]]
    try:
        snapshot = fetch(entry)
    except BlockwatchError as error:
        return _result(entry, chain, error=str(error), previous=previous,
                       rates=rates), previous
    except Exception:  # noqa: BLE001 - provider JSON is untrusted
        return _result(entry, chain, error="provider returned unusable data",
                       previous=previous, rates=rates), previous

    activities = list(snapshot.activities)[:MAX_EVENTS_PER_ADDRESS]
    initialized = previous is None
    fresh, truncated = _fresh_activities(snapshot, previous, activities)
    events = [_event_dict(activity, entry, chain, checked_at) for activity in fresh]
    result = _result(entry, chain, snapshot=snapshot, initialized=initialized,
                     events=events, truncated=truncated, previous=previous,
                     rates=rates)

    last_event_at = checked_at if events else (previous or {}).get("lastEventAt")
    state_entry = {
        "seen": remember_seen((previous or {}).get("seen", []),
                              [activity.id for activity in activities]),
        "balanceRaw": snapshot.balance_raw,
        "fingerprint": snapshot.fingerprint,
        "baselineAt": (previous or {}).get("baselineAt") or checked_at,
        "registered": snapshot.registered,
    }
    if last_event_at:
        state_entry["lastEventAt"] = last_event_at
    return result, state_entry


def _summary(result, checked_at):
    return {
        "balance": result["balance"],
        "balanceRaw": result["balanceRaw"],
        "balanceLabel": result["balanceLabel"],
        "status": result["status"],
        "error": result["error"],
        "note": result["note"],
        "registered": result["registered"],
        "checkedAt": checked_at,
        "lastEventAt": None,
    }


def _poll_one(entry, state, checked_at, rates):
    previous = state["addresses"].get(entry["id"])
    result, state_entry = _check(entry, previous, checked_at, rates)
    summary = _summary(result, checked_at)
    summary["lastEventAt"] = (state_entry or {}).get("lastEventAt") or (
        previous or {}).get("lastEventAt")
    state["lastResults"][entry["id"]] = summary
    error = None
    if result["error"]:
        error = {
            "id": entry["id"],
            "label": result["label"],
            "message": result["error"],
        }
    return result, state_entry, error


def poll(config_path=None, state_path=None, fiat="USD"):
    started = time.monotonic()
    config = load_config(config_path)
    state, warning = load_state(state_path)
    prune_state(state, config["addresses"])
    checked_at = _now()
    rates = rates_for(state, fiat, fetch=True)

    results = []
    all_events = []
    errors = []
    next_addresses = {}
    live_config = config
    for entry in config["addresses"]:
        with config_lock(config_path):
            # Re-read under the lock: a CLI removal must not be followed by a
            # request from this poll's older snapshot.
            live_config = load_config(config_path)
            live_entry = next(
                (item for item in live_config["addresses"]
                 if item["id"] == entry["id"]), None)
            if live_entry is None:
                continue
            result, state_entry, error = _poll_one(live_entry, state, checked_at, rates)
        results.append(result)
        if state_entry is not None:
            next_addresses[entry["id"]] = state_entry
        if error is not None:
            errors.append(error)
        all_events.extend(result["newEvents"])

    state["addresses"] = next_addresses
    state["checkedAt"] = checked_at
    save_warning = None
    try:
        save_state(state, state_path)
    except BlockwatchError as error:
        save_warning = str(error)

    return {
        "ok": True,
        "version": 1,
        "checkedAt": checked_at,
        "durationMs": int((time.monotonic() - started) * 1000),
        "warning": warning or save_warning,
        "addresses": results,
        "events": all_events[:MAX_REPORT_EVENTS],
        "errors": errors,
        "servers": server_list(live_config),
        "rates": rates,
        "fiatCurrencies": fiat_list(),
    }


def _stored_address(entry, last, stored, rates):
    chain = CHAINS[entry["chain"]]
    fiat = fiat_fields(last.get("balanceRaw"), chain.decimals, chain.symbol, rates)
    return {
        "id": entry["id"],
        "chain": chain.id,
        "name": chain.name,
        "symbol": chain.symbol,
        "label": entry.get("label") or "",
        "address": entry["address"],
        "mode": entry.get("mode"),
        "endpoint": entry.get("endpoint") if chain.id == "xmr" else None,
        "hasViewKey": bool(entry.get("viewKey")) if chain.id == "xmr" else False,
        "balance": last.get("balance"),
        "balanceRaw": last.get("balanceRaw"),
        "balanceLabel": last.get("balanceLabel", "balance"),
        "fiatValue": fiat["fiatValue"],
        "fiatCurrency": fiat["fiatCurrency"],
        "fiatSymbol": fiat["fiatSymbol"],
        "fiatDecimals": fiat["fiatDecimals"],
        "status": last.get("status", "unknown"),
        "error": last.get("error"),
        "note": last.get("note", ""),
        "registered": last.get("registered"),
        "checkedAt": last.get("checkedAt"),
        "lastEventAt": last.get("lastEventAt") or stored.get("lastEventAt"),
        "newEvents": [],
        "truncated": 0,
    }


def status(config_path=None, state_path=None, fiat="USD"):
    config = load_config(config_path)
    state, warning = load_state(state_path)
    removed = prune_state(state, config["addresses"])
    if removed:
        try:
            save_state(state, state_path)
        except BlockwatchError:
            pass
    rates = rates_for(state, fiat, fetch=False)
    addresses = [
        _stored_address(
            entry,
            state["lastResults"].get(entry["id"]) or {},
            state["addresses"].get(entry["id"]) or {},
            rates,
        )
        for entry in config["addresses"]
    ]
    return {
        "ok": True,
        "version": 1,
        "checkedAt": state.get("checkedAt"),
        "warning": warning,
        "addresses": addresses,
        "events": [],
        "errors": [],
        "chains": chain_list(),
        "servers": server_list(config),
        "rates": rates,
        "fiatCurrencies": fiat_list(),
    }
