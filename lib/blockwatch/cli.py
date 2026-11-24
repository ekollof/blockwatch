"""Command line entry point for the Blockwatch helper.

The QML bar calls `poll` on a timer and `status` at startup; the panel calls
`add` and `remove` when the user edits the watch list. Every command prints one
JSON object and never prompts, so it is safe to run from a host process.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__
from .config import (CHAINS, add_address, add_server, chain_list,
                     default_config_path, default_state_path, load_config,
                     remove_address, remove_server, server_list,
                     update_address)
from .rates import FIATS
from .util import BlockwatchError
from .watch import poll, status


def _parser():
    parser = argparse.ArgumentParser(
        prog="blockwatch",
        description="Read-only crypto address watcher for the Omarchy bar.")
    parser.add_argument("--config", default=None, help="config file path")
    parser.add_argument("--state", default=None, help="state file path")
    parser.add_argument("--fiat", default="USD", choices=sorted(FIATS),
                        help="fiat currency for balance estimates (default USD)")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("poll", help="check every watched address and report events")
    commands.add_parser("status", help="show config and the last check without network")
    commands.add_parser("list", help="list watched addresses")
    commands.add_parser("chains", help="list supported chains")
    commands.add_parser("version", help="print the helper version")

    add = commands.add_parser("add", help="watch another address")
    add.add_argument("--chain", required=True, choices=sorted(CHAINS))
    add.add_argument("--address", required=True)
    add.add_argument("--label", default="")
    add.add_argument("--view-key", default="", help="Monero private view key (LWS mode)")
    add.add_argument("--mode", choices=("lws", "wallet-rpc"), default=None,
                     help="Monero source; default is the configured light wallet server")
    add.add_argument("--endpoint", default="", help="override the provider endpoint")
    add.add_argument("--api-key", default="", help="provider API key, if required")
    add.add_argument("--rpc-user", default="", help="monero-wallet-rpc basic auth user")
    add.add_argument("--rpc-password", default="", help="monero-wallet-rpc basic auth password")

    update = commands.add_parser("update", help="change an existing watch")
    update.add_argument("identifier", help="address id or the address itself")
    update.add_argument("--chain", choices=sorted(CHAINS), default=None)
    update.add_argument("--address", default=None)
    update.add_argument("--label", default=None,
                        help="new label; pass an empty string to clear it")
    update.add_argument("--view-key", default=None,
                        help="replace the Monero view key; omit to keep it")
    update.add_argument("--mode", choices=("lws", "wallet-rpc"), default=None)
    update.add_argument("--endpoint", default=None)
    update.add_argument("--api-key", default=None)
    update.add_argument("--rpc-user", default=None)
    update.add_argument("--rpc-password", default=None)

    remove = commands.add_parser("remove", help="stop watching an address")
    remove.add_argument("identifier", help="address id or the address itself")

    commands.add_parser("servers", help="list light wallet servers and custom endpoints")
    server = commands.add_parser("server", help="manage custom XMR servers")
    server_commands = server.add_subparsers(dest="server_command", required=True)
    server_add = server_commands.add_parser("add", help="save a custom XMR server")
    server_add.add_argument("--label", default="")
    server_add.add_argument("--endpoint", required=True)
    server_add.add_argument("--mode", choices=("lws", "wallet-rpc"), default=None)
    server_add.add_argument("--note", default="")
    server_remove = server_commands.add_parser("remove", help="forget a custom XMR server")
    server_remove.add_argument("identifier", help="custom server id or endpoint")
    return parser


def _public_entry(entry):
    out = {
        "id": entry["id"],
        "chain": entry["chain"],
        "address": entry["address"],
        "label": entry.get("label") or "",
    }
    if entry.get("mode"):
        out["mode"] = entry["mode"]
    if entry.get("chain") == "xmr":
        out["endpoint"] = entry.get("endpoint") or ""
        out["hasViewKey"] = bool(entry.get("viewKey"))
    return out


def _run(args):
    config_path = args.config or default_config_path()
    state_path = args.state or default_state_path()

    if args.command == "poll":
        return poll(config_path, state_path, args.fiat)
    if args.command == "status":
        return status(config_path, state_path, args.fiat)
    if args.command == "list":
        document = load_config(config_path)
        return {"ok": True, "addresses": [_public_entry(item) for item in document["addresses"]]}
    if args.command == "chains":
        return {"ok": True, "chains": chain_list()}
    if args.command == "version":
        return {"ok": True, "name": "blockwatch", "version": __version__}
    if args.command == "servers":
        document = load_config(config_path)
        return {"ok": True, "servers": server_list(document)}
    if args.command == "server":
        if args.server_command == "add":
            server = add_server({
                "label": args.label,
                "endpoint": args.endpoint,
                "mode": args.mode,
                "note": args.note,
            }, config_path)
            return {"ok": True, "server": server}
        if args.server_command == "remove":
            server = remove_server(args.identifier, config_path)
            return {"ok": True, "removed": server}
    if args.command == "add":
        view_key = args.view_key or os.environ.get("BLOCKWATCH_VIEW_KEY", "")
        api_key = args.api_key or os.environ.get("BLOCKWATCH_API_KEY", "")
        rpc_user = args.rpc_user or os.environ.get("BLOCKWATCH_RPC_USER", "")
        rpc_pass = args.rpc_password or os.environ.get("BLOCKWATCH_RPC_PASSWORD", "")
        raw = {
            "chain": args.chain,
            "address": args.address,
            "label": args.label,
            "viewKey": view_key,
            "mode": args.mode,
            "endpoint": args.endpoint,
            "apiKey": api_key,
            "authUser": rpc_user,
            "authPass": rpc_pass,
        }
        raw = {key: value for key, value in raw.items() if value not in (None, "")}
        entry = add_address(raw, config_path)
        return {"ok": True, "entry": _public_entry(entry)}
    if args.command == "update":
        view_key = args.view_key or os.environ.get("BLOCKWATCH_VIEW_KEY") or None
        # Provider credentials are endpoint-bound. Do not reapply a global
        # environment token to a different endpoint on an update; require an
        # explicit --api-key/--rpc-* choice for the new provider.
        api_key = args.api_key
        rpc_user = args.rpc_user
        rpc_pass = args.rpc_password
        raw = {
            "chain": args.chain,
            "address": args.address,
            "label": args.label,
            "viewKey": view_key,
            "mode": args.mode,
            "endpoint": args.endpoint,
            "apiKey": api_key,
            "authUser": rpc_user,
            "authPass": rpc_pass,
        }
        raw = {key: value for key, value in raw.items() if value is not None}
        previous, entry = update_address(args.identifier, raw, config_path)
        return {"ok": True, "previous": _public_entry(previous),
                "entry": _public_entry(entry)}
    if args.command == "remove":
        entry = remove_address(args.identifier, config_path)
        return {"ok": True, "removed": _public_entry(entry)}
    raise BlockwatchError("unknown command")


def _emit(document):
    text = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
    try:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()
    except BrokenPipeError:
        pass


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        document = _run(args)
    except BlockwatchError as error:
        _emit({"ok": False, "error": str(error)})
        return 1
    except Exception as error:  # noqa: BLE001 - a helper must not dump a traceback into the bar
        _emit({"ok": False, "error": "internal error: " + type(error).__name__})
        return 1
    _emit(document)
    return 0


if __name__ == "__main__":
    sys.exit(main())
