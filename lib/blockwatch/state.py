"""Persistent watch state: what was already seen, and the last check result.

This file is what makes a notification mean "new since last time" instead of
"this address has transactions". It contains only ids, amounts and timestamps —
never a view key or any other credential.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import time

from . import __version__
from .config import default_state_path
from .rates import clean_cache
from .util import BlockwatchError, digit_string, iso_from_string, text

STATE_VERSION = 1
MAX_SEEN = 200


def empty_state():
    return {"version": STATE_VERSION, "checkedAt": None, "addresses": {},
            "lastResults": {}, "rates": None}


def _tighten(path, mode):
    try:
        info = os.lstat(path)
    except OSError:
        return
    if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
        return
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _clean_entry(raw):
    if not isinstance(raw, dict):
        return None
    entry = {}
    seen = raw.get("seen")
    if isinstance(seen, list):
        ids = []
        for item in seen:
            if isinstance(item, str) and item and len(item) <= 200:
                if all(ch >= " " for ch in item):
                    ids.append(item)
        entry["seen"] = ids[-MAX_SEEN:]
    else:
        entry["seen"] = []
    balance = raw.get("balanceRaw")
    if isinstance(balance, str) and balance.isdigit() and len(balance) <= 80:
        entry["balanceRaw"] = balance
    fingerprint = raw.get("fingerprint")
    if isinstance(fingerprint, str) and len(fingerprint) <= 500:
        entry["fingerprint"] = fingerprint
    for key in ("baselineAt", "lastEventAt"):
        value = iso_from_string(raw.get(key))
        if value:
            entry[key] = value
    if isinstance(raw.get("registered"), bool):
        entry["registered"] = raw["registered"]
    return entry


def _clean_addresses(raw_addresses):
    out = {}
    if not isinstance(raw_addresses, dict):
        return out
    for key, value in list(raw_addresses.items())[:200]:
        if isinstance(key, str) and len(key) <= 300:
            cleaned = _clean_entry(value)
            if cleaned is not None:
                out[key] = cleaned
    return out


def _clean_result(value):
    if not isinstance(value, dict):
        return {}
    result = {}
    for field in ("balance", "balanceRaw", "error", "note", "checkedAt", "status"):
        raw_value = value.get(field)
        if raw_value is None:
            continue
        if field == "balanceRaw":
            if isinstance(raw_value, str) and raw_value.isdigit() and len(raw_value) <= 80:
                result[field] = raw_value
        elif field == "checkedAt":
            parsed = iso_from_string(raw_value)
            if parsed:
                result[field] = parsed
        else:
            try:
                result[field] = text(raw_value, field, limit=300, allow_empty=True)
            except BlockwatchError:
                continue
    return result


def _clean_results(raw_results):
    out = {}
    if not isinstance(raw_results, dict):
        return out
    for key, value in list(raw_results.items())[:200]:
        if isinstance(key, str) and len(key) <= 300:
            out[key] = _clean_result(value)
    return out


def load_state(path=None):
    """Return (state, warning). A damaged file is moved aside, not trusted."""
    path = path or default_state_path()
    if not os.path.exists(path):
        return empty_state(), None
    directory = os.path.dirname(path)
    if directory:
        _tighten(directory, 0o700)
    _tighten(path, 0o600)
    try:
        if not stat.S_ISREG(os.stat(path).st_mode):
            raise BlockwatchError("state path is not a regular file")
    except OSError as error:
        raise BlockwatchError(f"cannot read state: {error.strerror or error}") from None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = handle.read(2_000_001)
    except OSError as error:
        raise BlockwatchError(f"cannot read state: {error.strerror or error}") from None
    if len(raw) > 2_000_000:
        raise BlockwatchError("state file is too large")
    try:
        document = json.loads(raw)
    except ValueError:
        document = None
    if not isinstance(document, dict) or document.get("version") != STATE_VERSION:
        aside = f"{path}.corrupt-{int(time.time())}"
        try:
            os.replace(path, aside)
            warning = f"state file was unreadable; moved aside to {os.path.basename(aside)}"
        except OSError:
            warning = "state file was unreadable; starting with a fresh baseline"
        return empty_state(), warning

    state = empty_state()
    state["checkedAt"] = iso_from_string(document.get("checkedAt"))
    state["addresses"] = _clean_addresses(document.get("addresses"))
    state["lastResults"] = _clean_results(document.get("lastResults"))
    state["rates"] = clean_cache(document.get("rates"))
    return state, None


def save_state(state, path=None):
    path = path or default_state_path()
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, mode=0o700, exist_ok=True)
    _tighten(directory, 0o700)
    document = {
        "version": STATE_VERSION,
        "checkedAt": state.get("checkedAt"),
        "addresses": state.get("addresses", {}),
        "lastResults": state.get("lastResults", {}),
        "rates": state.get("rates"),
        "writer": f"blockwatch/{__version__}",
    }
    payload = json.dumps(document, ensure_ascii=False, separators=(",", ":")) + "\n"
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=directory, prefix=".state-", delete=False)
    try:
        with handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(handle.name, 0o600)
        os.replace(handle.name, path)
    except OSError as error:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise BlockwatchError(f"cannot write state: {error.strerror or error}") from None


def prune_state(state, entries):
    keep = {entry["id"] for entry in entries}
    before = len(state["addresses"])
    state["addresses"] = {key: value for key, value in state["addresses"].items() if key in keep}
    state["lastResults"] = {key: value for key, value in state["lastResults"].items() if key in keep}
    return before - len(state["addresses"])


def remember_seen(previous, new_ids):
    seen = [item for item in previous if isinstance(item, str) and item]
    seen.extend(item for item in new_ids if isinstance(item, str) and item)
    out = []
    for item in seen:
        if item not in out:
            out.append(item)
    return out[-MAX_SEEN:]


def safe_balance(raw):
    try:
        return digit_string(raw, "balance")
    except BlockwatchError:
        return None
