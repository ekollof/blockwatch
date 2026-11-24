"""Watched-address configuration.

The config file is the single source of truth for what Blockwatch watches. It
is deliberately small, user-editable, and read-only in spirit: an entry names
a public address (plus, for Monero, a private view key when a light wallet
server is used) and nothing that can move funds.

    ~/.config/blockwatch/config.json          (or $XDG_CONFIG_HOME)

Writes are atomic and the file is kept at mode 0600 because a Monero view key
is a secret: it reveals incoming payments to whoever holds it.
"""

from __future__ import annotations

import fcntl
import json
import os
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import urlsplit

from .util import (BlockwatchError, ValidationError, address_key, clean_label,
                   plain, validate_address, validate_view_key)

CONFIG_VERSION = 1
MAX_ADDRESSES = 100


@dataclass(frozen=True)
class Chain:
    id: str
    name: str
    symbol: str
    decimals: int
    provider: str
    endpoint: str
    needs_view_key: bool = False
    note: str = ""


CHAINS = {
    chain.id: chain
    for chain in (
        Chain("btc", "Bitcoin", "BTC", 8, "mempool", "https://mempool.space",
              note="Public transactions; no key needed."),
        Chain("ltc", "Litecoin", "LTC", 8, "mempool", "https://litecoinspace.org",
              note="Public transactions; no key needed."),
        Chain("doge", "Dogecoin", "DOGE", 8, "blockcypher",
              "https://api.blockcypher.com/v1/doge/main",
              note="Public transactions; no key needed."),
        Chain("eth", "Ethereum", "ETH", 18, "blockscout", "https://eth.blockscout.com",
              note="Native ETH transfers; tokens are not followed."),
        Chain("sol", "Solana", "SOL", 9, "solana", "https://api.mainnet-beta.solana.com",
              note="Public signatures and balance; no key needed."),
        Chain("xmr", "Monero", "XMR", 12, "lws", "https://lws.monero.fail",
              needs_view_key=True,
              note="Monero hides addresses; watching needs the private view key."),
    )
}

DEFAULT_XMR_RPC = "http://127.0.0.1:18083/json_rpc"

XMR_MODES = ("lws", "wallet-rpc")
MAX_SERVERS = 20
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}

# Endpoints that can actually watch an XMR address. A public monerod does not
# index addresses; it belongs behind monero-wallet-rpc as its daemon, not here.
BUILTIN_SERVERS = (
    {
        "id": "lws-monero-fail",
        "label": "lws.monero.fail",
        "endpoint": "https://lws.monero.fail",
        "mode": "lws",
        "note": "Community light wallet server; wallet registration is required",
    },
    {
        "id": "wallet-rpc-local",
        "label": "Local monero-wallet-rpc",
        "endpoint": DEFAULT_XMR_RPC,
        "mode": "wallet-rpc",
        "note": "View-only wallet RPC on this machine",
    },
)


def config_home():
    return os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")


def state_home():
    return os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")


def default_config_path():
    return os.path.join(config_home(), "blockwatch", "config.json")


def default_state_path():
    return os.path.join(state_home(), "blockwatch", "state.json")


def _known_url(value, what, allow_query=True):
    value = str(value or "").strip().rstrip("/")
    if len(value) > 500:
        raise ValidationError(f"{what} is too long")
    if any(ch < " " or ch == "\x7f" for ch in value) or any(ch.isspace() for ch in value):
        raise ValidationError(f"{what} contains whitespace or control characters")
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValidationError(f"{what} must be an http(s) URL with a host")
    if parts.scheme == "http" and parts.hostname not in LOOPBACK_HOSTS:
        raise ValidationError(f"{what} must use https (http is for loopback only)")
    if parts.username or parts.password:
        raise ValidationError(f"{what} must not contain credentials")
    if not allow_query and (parts.query or parts.fragment):
        raise ValidationError(f"{what} must be a base URL without a query or fragment")
    return value


def _mode_for(chain_id, raw):
    if chain_id != "xmr":
        return None
    mode = raw.get("mode")
    if mode in XMR_MODES:
        return mode
    endpoint = str(raw.get("endpoint") or "")
    if endpoint.rstrip("/").endswith("/json_rpc"):
        return "wallet-rpc"
    return "lws"


def _entry_source(chain, raw, where):
    mode = _mode_for(chain.id, raw)
    endpoint = raw.get("endpoint")
    if endpoint:
        return mode, _known_url(endpoint, f"{where} endpoint", allow_query=chain.id != "xmr")
    if mode == "wallet-rpc":
        return mode, DEFAULT_XMR_RPC
    return mode, chain.endpoint


def _entry_secrets(raw, where):
    out = {}
    view_key = raw.get("viewKey")
    if view_key:
        out["viewKey"] = validate_view_key(view_key)
    for key in ("apiKey", "authUser", "authPass"):
        value = raw.get(key)
        if value is not None:
            value = str(value)
            if not value or len(value) > 200 or any(ch < " " for ch in value):
                raise ValidationError(f"{where} has an invalid {key}")
            out[key] = value
    if out.get("authPass") and not out.get("authUser"):
        raise ValidationError(f"{where} has an auth password without a user")
    return out


def normalize_entry(raw, index=None):
    where = f"address {index + 1}" if index is not None else "address"
    if not isinstance(raw, dict):
        raise ValidationError(f"{where} is not an object")
    chain_id = str(raw.get("chain") or "").strip().lower()
    chain = CHAINS.get(chain_id)
    if chain is None:
        raise ValidationError(f"{where} has unknown chain '{chain_id}'")
    address = validate_address(chain_id, raw.get("address"))
    mode, endpoint = _entry_source(chain, raw, where)
    secrets = _entry_secrets(raw, where)
    if chain_id != "xmr":
        secrets.pop("viewKey", None)
        secrets.pop("authUser", None)
        secrets.pop("authPass", None)
    if chain.needs_view_key and mode != "wallet-rpc" and "viewKey" not in secrets:
        raise ValidationError(
            f"{where} needs the Monero private view key, or mode 'wallet-rpc'")

    entry = {
        "id": address_key(chain_id, address),
        "chain": chain_id,
        "address": address,
        "label": clean_label(raw.get("label")),
        "endpoint": endpoint,
    }
    if mode:
        entry["mode"] = mode
    entry.update(secrets)
    return entry


def normalize_server(raw):
    if not isinstance(raw, dict):
        raise ValidationError("server is not an object")
    endpoint = _known_url(raw.get("endpoint"), "server endpoint", allow_query=False)
    mode = raw.get("mode")
    if mode not in XMR_MODES:
        mode = "wallet-rpc" if endpoint.rstrip("/").endswith("/json_rpc") else "lws"
    label = clean_label(raw.get("label"), fallback=urlsplit(endpoint).netloc or endpoint)
    note = ""
    if raw.get("note"):
        note = plain(raw["note"], 120)
    identifier = str(raw.get("id") or endpoint)
    if not identifier or len(identifier) > 300:
        identifier = endpoint
    entry = {
        "id": identifier,
        "label": label,
        "endpoint": endpoint,
        "mode": mode,
        "custom": bool(raw.get("custom", True)),
    }
    if note:
        entry["note"] = note
    return entry


def server_list(document):
    """Built-in servers plus the endpoints this user has added."""
    out = []
    seen = set()
    for item in BUILTIN_SERVERS:
        entry = dict(item)
        entry["custom"] = False
        out.append(entry)
        seen.add((entry["endpoint"], entry["mode"]))
    for item in document.get("servers", []) if isinstance(document, dict) else []:
        try:
            entry = normalize_server(item)
        except ValidationError:
            continue
        key = (entry["endpoint"], entry["mode"])
        if key in seen:
            continue
        entry["custom"] = True
        out.append(entry)
        seen.add(key)
    return out


def remember_server(document, entry):
    """Keep a custom XMR endpoint so it is offered next time."""
    if entry.get("chain") != "xmr" or not entry.get("endpoint"):
        return False
    mode = entry.get("mode") or "lws"
    endpoint = entry["endpoint"]
    if any(server["endpoint"] == endpoint and server["mode"] == mode
           for server in BUILTIN_SERVERS):
        return False
    servers = document.setdefault("servers", [])
    for server in servers:
        if server.get("endpoint") == endpoint and server.get("mode") == mode:
            return False
    try:
        server = normalize_server({
            "id": endpoint,
            "label": urlsplit(endpoint).netloc or endpoint,
            "endpoint": endpoint,
            "mode": mode,
        })
    except ValidationError:
        return False
    servers.append(server)
    del servers[:-MAX_SERVERS]
    return True


def empty_config():
    return {"version": CONFIG_VERSION, "addresses": [], "servers": []}


def _tighten(path, mode):
    """Chmod a regular file or real directory without following a symlink."""
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


@contextmanager
def config_lock(path=None):
    """Serialize config reads/edits against the poll's per-address checks.

    The poll holds this lock while it reloads the config and performs one
    address's network checks. A CLI add/update/remove therefore cannot report
    success while an older poll snapshot is still about to contact the removed
    watch. The lock file lives beside the config, is 0600, and is never a
    symlink.
    """
    target = path or default_config_path()
    directory = os.path.dirname(target) or "."
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        _tighten(directory, 0o700)
        lock_path = target + ".lock"
        flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(lock_path, flags, 0o600)
    except OSError as error:
        raise BlockwatchError(f"cannot lock config: {error.strerror or error}") from None
    try:
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            pass
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def load_config(path=None):
    path = path or default_config_path()
    if not os.path.exists(path):
        return empty_config()
    directory = os.path.dirname(path)
    if directory:
        _tighten(directory, 0o700)
    _tighten(path, 0o600)
    try:
        if not stat.S_ISREG(os.stat(path).st_mode):
            raise BlockwatchError("config path is not a regular file")
    except OSError as error:
        raise BlockwatchError(f"cannot read config: {error.strerror or error}") from None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = handle.read(1_000_001)
    except OSError as error:
        raise BlockwatchError(f"cannot read config: {error.strerror or error}") from None
    if len(raw) > 1_000_000:
        raise BlockwatchError("config file is too large")
    try:
        document = json.loads(raw)
    except ValueError:
        raise BlockwatchError("config file is not valid JSON") from None
    if not isinstance(document, dict) or document.get("version") != CONFIG_VERSION:
        raise BlockwatchError("config file has an unsupported version")
    entries = document.get("addresses", [])
    if not isinstance(entries, list):
        raise BlockwatchError("config 'addresses' is not a list")
    if len(entries) > MAX_ADDRESSES:
        raise BlockwatchError(f"config has more than {MAX_ADDRESSES} addresses")
    normalized = []
    seen = set()
    for index, item in enumerate(entries):
        entry = normalize_entry(item, index)
        if entry["id"] in seen:
            raise BlockwatchError(f"config watches {entry['address']} twice")
        seen.add(entry["id"])
        normalized.append(entry)

    servers = []
    raw_servers = document.get("servers", [])
    if isinstance(raw_servers, list):
        seen_servers = set()
        for item in raw_servers[:MAX_SERVERS]:
            try:
                server = normalize_server(item)
            except ValidationError:
                continue
            key = (server["endpoint"], server["mode"])
            if key in seen_servers:
                continue
            seen_servers.add(key)
            servers.append(server)
    return {"version": CONFIG_VERSION, "addresses": normalized, "servers": servers}


def save_config(document, path=None):
    path = path or default_config_path()
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, mode=0o700, exist_ok=True)
    _tighten(directory, 0o700)
    payload = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=directory, prefix=".config-", delete=False)
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
        raise BlockwatchError(f"cannot write config: {error.strerror or error}") from None


def add_address(raw, path=None):
    with config_lock(path):
        document = load_config(path)
        entry = normalize_entry(raw)
        if any(item["id"] == entry["id"] for item in document["addresses"]):
            raise BlockwatchError("that address is already watched")
        if len(document["addresses"]) >= MAX_ADDRESSES:
            raise BlockwatchError(f"at most {MAX_ADDRESSES} addresses can be watched")
        document["addresses"].append(entry)
        remember_server(document, entry)
        save_config(document, path)
        return entry


def update_address(identifier, raw, path=None):
    """Replace one watch, preserving secrets the caller did not resend.

    The panel edits a watch without ever receiving its view key, so a blank
    view key means "keep the stored one". If the address or chain changes the
    old key cannot be assumed to belong to the new target and is dropped.
    """
    with config_lock(path):
        return _update_address_unlocked(identifier, raw, path)


def _update_address_unlocked(identifier, raw, path=None):
    """Replace one watch. Caller holds config_lock."""
    document = load_config(path)
    needle = str(identifier or "").strip()
    index = None
    existing = None
    for position, entry in enumerate(document["addresses"]):
        if entry["id"] == needle or entry["address"].lower() == needle.lower():
            index = position
            existing = entry
            break
    if existing is None:
        raise BlockwatchError("that address is not watched")

    merged = {
        key: existing[key]
        for key in ("chain", "address", "label", "mode", "endpoint",
                    "viewKey", "apiKey", "authUser", "authPass")
        if existing.get(key) is not None
    }
    chain_changed = "chain" in raw and raw["chain"] != existing["chain"]
    endpoint_changed = "endpoint" in raw and raw["endpoint"] != existing.get("endpoint")
    mode_changed = "mode" in raw and raw["mode"] != existing.get("mode")
    if chain_changed:
        for key in ("endpoint", "mode", "viewKey", "apiKey", "authUser", "authPass"):
            merged.pop(key, None)
    if endpoint_changed or mode_changed:
        # Provider credentials belong to the endpoint they were entered for.
        for key in ("apiKey", "authUser", "authPass"):
            merged.pop(key, None)
    target_changed = (
        chain_changed
        or ("address" in raw and str(raw["address"]).strip().lower()
            != existing["address"].lower())
    )
    if target_changed:
        if not raw.get("viewKey"):
            merged.pop("viewKey", None)
        if not raw.get("apiKey"):
            merged.pop("apiKey", None)
    if "mode" in raw and raw["mode"] != existing.get("mode") and "endpoint" not in raw:
        merged.pop("endpoint", None)
    if "mode" not in raw and "endpoint" in raw:
        endpoint = str(raw["endpoint"] or "").rstrip("/")
        if endpoint.endswith("/json_rpc"):
            merged["mode"] = "wallet-rpc"
        elif existing.get("mode") == "wallet-rpc":
            merged["mode"] = "lws"
    for key, value in raw.items():
        if value is not None:
            merged[key] = value

    entry = normalize_entry(merged)
    if any(item["id"] == entry["id"] and position != index
           for position, item in enumerate(document["addresses"])):
        raise BlockwatchError("that address is already watched")
    document["addresses"][index] = entry
    remember_server(document, entry)
    save_config(document, path)
    return existing, entry


def add_server(raw, path=None):
    with config_lock(path):
        return _add_server_unlocked(raw, path)


def _add_server_unlocked(raw, path=None):
    document = load_config(path)
    server = normalize_server(raw)
    server["custom"] = True
    existing = server_list(document)
    if any(item["endpoint"] == server["endpoint"] and item["mode"] == server["mode"]
           for item in existing):
        raise BlockwatchError("that server is already listed")
    if len(document["servers"]) >= MAX_SERVERS:
        raise BlockwatchError(f"at most {MAX_SERVERS} custom servers can be saved")
    document["servers"].append(server)
    save_config(document, path)
    return server


def remove_server(identifier, path=None):
    with config_lock(path):
        return _remove_server_unlocked(identifier, path)


def _remove_server_unlocked(identifier, path=None):
    document = load_config(path)
    needle = str(identifier or "").strip()
    if any(item["id"] == needle or item["endpoint"] == needle for item in BUILTIN_SERVERS):
        raise BlockwatchError("that server is built in and cannot be removed")
    removed = None
    kept = []
    for server in document["servers"]:
        if removed is None and (server["id"] == needle or server["endpoint"] == needle):
            removed = server
        else:
            kept.append(server)
    if removed is None:
        raise BlockwatchError("that server is not in the custom list")
    document["servers"] = kept
    save_config(document, path)
    return removed


def remove_address(identifier, path=None):
    with config_lock(path):
        return _remove_address_unlocked(identifier, path)


def _remove_address_unlocked(identifier, path=None):
    document = load_config(path)
    needle = str(identifier or "").strip()
    if not needle:
        raise BlockwatchError("give an address id or the address itself")
    removed = None
    kept = []
    for entry in document["addresses"]:
        matches = entry["id"] == needle or entry["address"].lower() == needle.lower()
        if matches and removed is None:
            removed = entry
        else:
            kept.append(entry)
    if removed is None:
        raise BlockwatchError("that address is not watched")
    document["addresses"] = kept
    save_config(document, path)
    return removed


def chain_list():
    return [
        {
            "id": chain.id,
            "name": chain.name,
            "symbol": chain.symbol,
            "decimals": chain.decimals,
            "needsViewKey": chain.needs_view_key,
            "note": chain.note,
            "endpoint": chain.endpoint,
        }
        for chain in CHAINS.values()
    ]
