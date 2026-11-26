"""Unit tests for the Blockwatch helper.

Network access is never used here: provider parsers are tested with recorded
shapes, and the poll tests replace `blockwatch.watch.fetch`.
"""

import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "lib"))

from blockwatch import http, providers, rates, watch  # noqa: E402
from blockwatch import cli  # noqa: E402
from blockwatch.config import (add_address, add_server, load_config,  # noqa: E402
                               remove_address, remove_server, save_config,
                               server_list, update_address, config_lock)
from blockwatch.providers import Activity, Snapshot  # noqa: E402
from blockwatch.state import load_state  # noqa: E402
from blockwatch.util import (BlockwatchError, ValidationError, format_units,  # noqa: E402
                             plain, validate_address)


BTC_ADDRESS = "bc1qgdjqv0av3q56jvd82tkdjpy7gdp9ut8tlqmgrpmv24sq90ecnvqqjwvw97"
SOL_ADDRESS = "grsdZ8zj8RzuUiEeK2qKrTcd9CdJBxjVbxxAhHPzeFpY"
# Shape-valid placeholders only: no real wallet keys belong in this repository.
XMR_ADDRESS = "4" + "A" * 94
XMR_VIEW_KEY = "ab" * 32
LIGHTNING_ADDRESS = "watchdemo@blink.sv"
BLINK_API_KEY = "blink_placeholder_read_only_key"
FAKE_RATES = {"BTC": "100000", "LTC": "120", "DOGE": "0.4",
              "ETH": "4000", "SOL": "200", "XMR": "300"}


class TempFiles(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.config = os.path.join(self.directory.name, "config.json")
        self.state = os.path.join(self.directory.name, "state.json")

    def tearDown(self):
        self.directory.cleanup()


class AmountTests(unittest.TestCase):
    def test_satoshi_formatting_trims_zeros(self):
        self.assertEqual(format_units("123456", 8), "0.00123456")
        self.assertEqual(format_units("100000000", 8), "1")

    def test_wei_stays_exact(self):
        self.assertEqual(format_units("11710000000000", 18), "0.00001171")
        self.assertEqual(format_units("5799564122593029130", 18),
                         "5.79956412259302913")

    def test_zero(self):
        self.assertEqual(format_units("0", 12), "0")

    def test_plain_text_strips_controls_and_bidi_overrides(self):
        self.assertEqual(plain("safe\u202eevil\x07"), "safeevil")
        self.assertEqual(plain("  spaced  "), "spaced")


class AddressValidationTests(unittest.TestCase):
    def test_valid_addresses(self):
        self.assertEqual(validate_address("btc", BTC_ADDRESS), BTC_ADDRESS)
        self.assertTrue(validate_address("eth", "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"))
        self.assertTrue(validate_address("sol",
                                        "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"))

    def test_rejects_garbage(self):
        for chain, value in (("btc", "not-an-address"), ("eth", "0x1234"),
                             ("doge", "0abc")):
            with self.assertRaises(ValidationError):
                validate_address(chain, value)

    def test_lightning_address_is_canonicalised(self):
        self.assertEqual(validate_address("lightning", "Kollof"),
                         "kollof@blink.sv")
        self.assertEqual(validate_address("lightning", "Kollof@BLINK.SV"),
                         "kollof@blink.sv")
        for value in ("kollof@example.com", "bad name", "@blink.sv", ".name@blink.sv"):
            with self.assertRaises(ValidationError):
                validate_address("lightning", value)


class ConfigTests(TempFiles):
    def test_add_and_remove(self):
        entry = add_address({"chain": "btc", "address": BTC_ADDRESS,
                             "label": "Cold"}, self.config)
        self.assertEqual(entry["label"], "Cold")
        self.assertEqual(load_config(self.config)["addresses"][0]["id"], entry["id"])
        removed = remove_address(entry["id"], self.config)
        self.assertEqual(removed["id"], entry["id"])
        self.assertEqual(load_config(self.config)["addresses"], [])

    def test_duplicate_is_refused(self):
        add_address({"chain": "btc", "address": BTC_ADDRESS}, self.config)
        with self.assertRaises(BlockwatchError):
            add_address({"chain": "btc", "address": BTC_ADDRESS}, self.config)

    def test_monero_needs_view_key_unless_wallet_rpc(self):
        with self.assertRaises(ValidationError):
            add_address({"chain": "xmr", "address": XMR_ADDRESS}, self.config)
        entry = add_address({"chain": "xmr", "address": XMR_ADDRESS,
                             "viewKey": XMR_VIEW_KEY}, self.config)
        self.assertEqual(entry["mode"], "lws")
        rpc = add_address({"chain": "xmr", "address":
                           "4" + "B" * 94,
                           "mode": "wallet-rpc"}, self.config)
        self.assertEqual(rpc["endpoint"], "http://127.0.0.1:18083/json_rpc")

    def test_private_mode(self):
        add_address({"chain": "btc", "address": BTC_ADDRESS}, self.config)
        self.assertEqual(stat.S_IMODE(os.stat(self.config).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.directory.name).st_mode), 0o700)

    def test_loading_tightens_loose_permissions(self):
        add_address({"chain": "xmr", "address": XMR_ADDRESS,
                     "viewKey": XMR_VIEW_KEY}, self.config)
        os.chmod(self.config, 0o644)
        load_config(self.config)
        self.assertEqual(stat.S_IMODE(os.stat(self.config).st_mode), 0o600)

    def test_status_never_returns_the_view_key(self):
        add_address({"chain": "xmr", "address": XMR_ADDRESS,
                     "viewKey": XMR_VIEW_KEY}, self.config)
        rendered = json.dumps(watch.status(self.config, self.state))
        self.assertNotIn(XMR_VIEW_KEY, rendered)
        self.assertNotIn("viewKey", rendered)

    def test_lightning_needs_a_read_only_api_key(self):
        with self.assertRaises(ValidationError):
            add_address({"chain": "lightning", "address": LIGHTNING_ADDRESS},
                        self.config)
        entry = add_address({"chain": "lightning", "address": "WatchDemo",
                             "apiKey": BLINK_API_KEY}, self.config)
        self.assertEqual(entry["address"], LIGHTNING_ADDRESS)
        self.assertEqual(entry["id"], "lightning:" + LIGHTNING_ADDRESS)
        self.assertEqual(entry["endpoint"], "https://api.blink.sv/graphql")

    def test_lightning_key_is_not_in_status(self):
        add_address({"chain": "lightning", "address": LIGHTNING_ADDRESS,
                     "apiKey": BLINK_API_KEY}, self.config)
        result = watch.status(self.config, self.state)
        rendered = json.dumps(result)
        self.assertNotIn(BLINK_API_KEY, rendered)
        self.assertNotIn("apiKey", rendered)
        self.assertTrue(result["addresses"][0]["hasApiKey"])


class ProviderParserTests(unittest.TestCase):
    def test_mempool_uses_net_flow(self):
        tx = {
            "txid": "a" * 64,
            "status": {"confirmed": True, "block_time": 1700000000},
            "vin": [{"prevout": {"scriptpubkey_address": BTC_ADDRESS, "value": 1000}}],
            "vout": [
                {"scriptpubkey_address": "bc1qother", "value": 400},
                {"scriptpubkey_address": BTC_ADDRESS, "value": 500},
            ],
        }
        activity = providers._mempool_activity(tx, BTC_ADDRESS)
        self.assertEqual(activity.kind, "out")
        self.assertEqual(activity.amount_raw, "500")
        self.assertTrue(activity.confirmed)

    def test_mempool_ignores_unrelated_transaction(self):
        tx = {"txid": "a" * 64, "status": {"confirmed": False},
              "vin": [], "vout": [{"scriptpubkey_address": "other", "value": 1}]}
        self.assertIsNone(providers._mempool_activity(tx, BTC_ADDRESS))

    def test_blockcypher_direction(self):
        tx = {
            "hash": "b" * 64,
            "confirmations": 3,
            "confirmed": "2026-11-20T05:19:41Z",
            "inputs": [{"addresses": ["elsewhere"], "output_value": 100}],
            "outputs": [{"addresses": ["DH5yaieqoZN36fDVciNyRueRGvGLR3mr7L"], "value": 50}],
        }
        activity = providers._blockcypher_activity(tx, "DH5yaieqoZN36fDVciNyRueRGvGLR3mr7L")
        self.assertEqual(activity.kind, "in")
        self.assertEqual(activity.amount_raw, "50")
        self.assertEqual(activity.confirmations, 3)

    def test_blockcypher_ignores_non_list_addresses(self):
        tx = {
            "hash": "b" * 64,
            "confirmations": 1,
            "inputs": [{"addresses": 5, "output_value": 100}],
            "outputs": [{"addresses": "not-a-list", "value": 50}],
        }
        self.assertIsNone(providers._blockcypher_activity(
            tx, "DH5yaieqoZN36fDVciNyRueRGvGLR3mr7L"))

    def test_blockscout_native_transfer(self):
        item = {
            "hash": "0x" + "c" * 64,
            "value": "11710000000000",
            "from": {"hash": "0x1111111111111111111111111111111111111111"},
            "to": {"hash": "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"},
            "status": "ok",
            "block_number": 123,
            "timestamp": "2026-11-22T11:16:23.000000Z",
        }
        activity = providers._blockscout_activity(
            item, "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045")
        self.assertEqual(activity.kind, "in")
        self.assertEqual(activity.amount_raw, "11710000000000")

    def test_monero_lws_transaction(self):
        item = {
            "hash": "d" * 64,
            "total_received": "2910210000",
            "total_sent": "0",
            "timestamp": "2026-10-23T02:04:33Z",
            "height": 3790034,
            "mempool": False,
        }
        activity = providers._xmr_lws_activity(item)
        self.assertEqual(activity.kind, "in")
        self.assertEqual(activity.amount_raw, "2910210000")

    def test_lws_empty_wallet_names_the_scan_start(self):
        info = {
            "total_received": "0",
            "total_sent": "0",
            "scanned_block_height": 3815001,
            "blockchain_height": 3815001,
            "start_height": 3812946,
        }
        received, _, progress, notes = providers._lws_summary(info)
        self.assertEqual(received, "0")
        self.assertEqual(progress, 100)
        self.assertTrue(any("block 3812946" in note for note in notes))

    def test_lws_wallet_with_receipts_has_no_scan_start_note(self):
        info = {
            "total_received": "1000000000000",
            "total_sent": "0",
            "scanned_block_height": 3815001,
            "blockchain_height": 3815001,
            "start_height": 3812946,
        }
        _, _, _, notes = providers._lws_summary(info)
        self.assertFalse(any("began scanning" in note for note in notes))

    @mock.patch("blockwatch.providers._solana_rpc")
    def test_solana_signatures_become_activity(self, rpc):
        rpc.side_effect = [{"value": 42}, [{
            "signature": "S" * 64,
            "blockTime": 1700000000,
            "confirmationStatus": "finalized",
            "err": None,
        }]]
        entry = {"chain": "sol", "address":
                 "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM",
                 "endpoint": "https://api.mainnet-beta.solana.com"}
        snapshot = providers._fetch_solana(entry, providers.CHAINS["sol"])
        self.assertEqual(snapshot.balance_raw, "42")
        self.assertEqual(len(snapshot.activities), 1)
        self.assertEqual(snapshot.activities[0].kind, "activity")


BLINK_RESPONSE = {
    "data": {
        "me": {
            "username": "watchdemo",
            "defaultAccount": {
                "wallets": [
                    {"__typename": "UsdWallet", "walletCurrency": "USD",
                     "balance": 1200},
                    {"__typename": "BTCWallet", "walletCurrency": "BTC",
                     "balance": 2500, "transactions": {"edges": [
                         {"node": {
                             "id": "tx-new", "status": "SUCCESS",
                             "direction": "RECEIVE", "settlementAmount": 1500,
                             "createdAt": 1700000000,
                             "initiationVia": {"__typename": "InitiationViaLn"},
                         }},
                         {"node": {
                             "id": "tx-pending", "status": "PENDING",
                             "direction": "RECEIVE", "settlementAmount": 50,
                             "createdAt": 1700000100,
                             "initiationVia": {"__typename": "InitiationViaLn"},
                         }},
                         {"node": {
                             "id": "tx-send", "status": "SUCCESS",
                             "direction": "SEND", "settlementAmount": -300,
                             "createdAt": 1699999000,
                             "initiationVia": {"__typename": "InitiationViaOnChain"},
                         }},
                         {"node": {
                             "id": "tx-failed", "status": "FAILURE",
                             "direction": "RECEIVE", "settlementAmount": 99,
                             "createdAt": 1699998000,
                             "initiationVia": {"__typename": "InitiationViaLn"},
                         }},
                     ]}},
                ]
            },
        }
    }
}


class BlinkProviderTests(unittest.TestCase):
    def entry(self):
        return {
            "chain": "lightning",
            "address": LIGHTNING_ADDRESS,
            "apiKey": BLINK_API_KEY,
            "endpoint": "https://api.blink.sv/graphql",
        }

    def test_wallet_balance_and_settled_transactions(self):
        with mock.patch("blockwatch.providers.http.request_json",
                        return_value=BLINK_RESPONSE) as request:
            snapshot = providers._fetch_blink(self.entry(),
                                              providers.CHAINS["lightning"])
        self.assertEqual(snapshot.balance_raw, "2500")
        self.assertEqual([activity.id for activity in snapshot.activities],
                         ["tx-new", "tx-send"])
        self.assertEqual(snapshot.activities[0].kind, "in")
        self.assertEqual(snapshot.activities[0].amount_raw, "1500")
        self.assertEqual(snapshot.activities[0].note, "lightning")
        self.assertEqual(snapshot.activities[1].kind, "out")
        self.assertEqual(snapshot.activities[1].amount_raw, "300")
        self.assertEqual(snapshot.activities[1].note, "on-chain")
        self.assertIn("1 pending", snapshot.note)
        _, kwargs = request.call_args
        self.assertEqual(kwargs["headers"]["X-API-KEY"], BLINK_API_KEY)
        self.assertEqual(kwargs["method"], "POST")

    def test_key_for_another_account_is_refused(self):
        response = json.loads(json.dumps(BLINK_RESPONSE))
        response["data"]["me"]["username"] = "someoneelse"
        with mock.patch("blockwatch.providers.http.request_json",
                        return_value=response):
            with self.assertRaises(BlockwatchError):
                providers._fetch_blink(self.entry(), providers.CHAINS["lightning"])

    def test_graphql_error_is_a_readable_failure(self):
        error = {"errors": [{"message": "Not authorized"}]}
        with mock.patch("blockwatch.providers.http.request_json",
                        return_value=error):
            with self.assertRaisesRegex(BlockwatchError, "Not authorized"):
                providers._fetch_blink(self.entry(), providers.CHAINS["lightning"])


class UrlTests(unittest.TestCase):
    def test_https_allowed(self):
        self.assertEqual(http.checked_url("https://mempool.space"),
                         "https://mempool.space")

    def test_http_only_for_loopback(self):
        self.assertEqual(http.checked_url("http://127.0.0.1:18083/json_rpc"),
                         "http://127.0.0.1:18083/json_rpc")
        with self.assertRaises(BlockwatchError):
            http.checked_url("http://example.com/api")

    def test_credentials_refused(self):
        with self.assertRaises(BlockwatchError):
            http.checked_url("https://user:pass@example.com")


class PollTests(TempFiles):
    def setUp(self):
        super().setUp()
        patcher = mock.patch("blockwatch.rates.fetch_rates", return_value=FAKE_RATES)
        patcher.start()
        self.addCleanup(patcher.stop)

    def add_btc(self):
        add_address({"chain": "btc", "address": BTC_ADDRESS, "label": "Cold"},
                    self.config)

    def test_first_poll_is_a_silent_baseline(self):
        self.add_btc()
        snapshot = Snapshot(balance_raw="1000", activities=[
            Activity("tx1", "in", "500", True, True, "2026-11-20T00:00:00Z")])
        with mock.patch("blockwatch.watch.fetch", return_value=snapshot):
            report = watch.poll(self.config, self.state)
        self.assertEqual(report["events"], [])
        self.assertTrue(report["addresses"][0]["initialized"])
        self.assertEqual(report["addresses"][0]["balance"], "0.00001")
        self.assertEqual(report["addresses"][0]["fiatValue"], "1.00")
        self.assertEqual(report["rates"]["currency"], "USD")
        status = watch.status(self.config, self.state, "USD")
        self.assertEqual(status["addresses"][0]["fiatValue"], "1.00")

    def test_new_transaction_notifies_once(self):
        self.add_btc()
        first = Snapshot(balance_raw="1000", activities=[
            Activity("tx1", "in", "500", True, True, "2026-11-20T00:00:00Z")])
        second = Snapshot(balance_raw="2000", activities=[
            Activity("tx2", "in", "1000", True, True, "2026-11-21T00:00:00Z"),
            Activity("tx1", "in", "500", True, True, "2026-11-20T00:00:00Z")])
        with mock.patch("blockwatch.watch.fetch", return_value=first):
            watch.poll(self.config, self.state)
        with mock.patch("blockwatch.watch.fetch", return_value=second):
            report = watch.poll(self.config, self.state)
        self.assertEqual([event["id"] for event in report["events"]], ["tx2"])
        with mock.patch("blockwatch.watch.fetch", return_value=second):
            report = watch.poll(self.config, self.state)
        self.assertEqual(report["events"], [])

    def test_balance_only_change_is_one_event(self):
        self.add_btc()
        with mock.patch("blockwatch.watch.fetch",
                        return_value=Snapshot(balance_raw="1000")):
            watch.poll(self.config, self.state)
        with mock.patch("blockwatch.watch.fetch",
                        return_value=Snapshot(balance_raw="1500")):
            report = watch.poll(self.config, self.state)
        self.assertEqual(len(report["events"]), 1)
        self.assertEqual(report["events"][0]["kind"], "in")
        self.assertEqual(report["events"][0]["amount"], "0.000005")

    def test_lightning_balance_is_priced_as_btc_and_events_use_sats(self):
        add_address({"chain": "lightning", "address": LIGHTNING_ADDRESS,
                     "apiKey": BLINK_API_KEY}, self.config)
        first = Snapshot(balance_raw="2500", activities=[
            Activity("tx1", "in", "1500", True, True, "2026-11-20T00:00:00Z")])
        with mock.patch("blockwatch.watch.fetch", return_value=first):
            report = watch.poll(self.config, self.state)
        row = report["addresses"][0]
        self.assertEqual(row["symbol"], "sats")
        self.assertEqual(row["balance"], "2500")
        self.assertEqual(row["fiatValue"], "2.50")
        self.assertTrue(row["hasApiKey"])
        self.assertNotIn(BLINK_API_KEY, json.dumps(report))

        second = Snapshot(balance_raw="3000", activities=[
            Activity("tx2", "in", "500", True, True, "2026-11-21T00:00:00Z"),
            Activity("tx1", "in", "1500", True, True, "2026-11-20T00:00:00Z")])
        with mock.patch("blockwatch.watch.fetch", return_value=second):
            report = watch.poll(self.config, self.state)
        self.assertEqual([event["id"] for event in report["events"]], ["tx2"])
        self.assertEqual(report["events"][0]["amount"], "500")
        self.assertEqual(report["events"][0]["symbol"], "sats")
        self.assertEqual(report["addresses"][0]["fiatValue"], "3.00")

    def test_failed_check_keeps_the_baseline(self):
        self.add_btc()
        first = Snapshot(balance_raw="1000", activities=[
            Activity("tx1", "in", "500", True, True, None)])
        with mock.patch("blockwatch.watch.fetch", return_value=first):
            watch.poll(self.config, self.state)
        with mock.patch("blockwatch.watch.fetch",
                        side_effect=BlockwatchError("network timeout")):
            report = watch.poll(self.config, self.state)
        self.assertEqual(report["errors"][0]["message"], "network timeout")
        with mock.patch("blockwatch.watch.fetch", return_value=first):
            report = watch.poll(self.config, self.state)
        self.assertEqual(report["events"], [])

    def test_unexpected_provider_shape_does_not_abort_the_report(self):
        self.add_btc()
        add_address({"chain": "ltc", "address":
                     "LdP8Qox1VAhCzLJNqrr74YovaWYyNBUWvL"}, self.config)
        good = Snapshot(balance_raw="1000")
        with mock.patch("blockwatch.watch.fetch",
                        side_effect=[TypeError("bad provider data"), good]):
            report = watch.poll(self.config, self.state)
        self.assertEqual(len(report["addresses"]), 2)
        self.assertEqual(report["addresses"][0]["error"],
                         "provider returned unusable data")
        self.assertEqual(report["addresses"][1]["status"], "ok")

    def test_corrupt_state_is_moved_aside(self):
        with open(self.state, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        state, warning = load_state(self.state)
        self.assertIsNotNone(warning)
        self.assertTrue(os.path.exists(self.state) is False)
        self.assertEqual(state["addresses"], {})


class ServerTests(TempFiles):
    def test_builtins_are_listed(self):
        document = load_config(self.config)
        labels = {server["label"] for server in server_list(document)}
        self.assertIn("lws.monero.fail", labels)
        self.assertIn("Local monero-wallet-rpc", labels)

    def test_custom_server_round_trip(self):
        add_server({"label": "My LWS", "endpoint": "https://lws.example.org"}, self.config)
        labels = {server["label"] for server in server_list(load_config(self.config))}
        self.assertIn("My LWS", labels)
        with self.assertRaises(BlockwatchError):
            add_server({"endpoint": "https://lws.example.org"}, self.config)
        remove_server("https://lws.example.org", self.config)
        labels = {server["label"] for server in server_list(load_config(self.config))}
        self.assertNotIn("My LWS", labels)

    def test_builtin_server_is_not_removable(self):
        with self.assertRaises(BlockwatchError):
            remove_server("lws-monero-fail", self.config)

    def test_http_is_loopback_only(self):
        with self.assertRaises(ValidationError):
            add_server({"endpoint": "http://lws.example.org"}, self.config)
        add_server({"endpoint": "http://127.0.0.1:18084/json_rpc"}, self.config)

    def test_server_urls_cannot_carry_a_query(self):
        with self.assertRaises(ValidationError):
            add_server({"endpoint": "https://lws.example.org/?token=secret"}, self.config)
        with self.assertRaises(ValidationError):
            add_address({"chain": "xmr", "address": XMR_ADDRESS,
                         "viewKey": XMR_VIEW_KEY,
                         "endpoint": "https://lws.example.org/?token=secret"},
                        self.config)

    def test_custom_xmr_endpoint_is_remembered(self):
        add_address({"chain": "xmr", "address": XMR_ADDRESS,
                     "viewKey": XMR_VIEW_KEY,
                     "endpoint": "https://lws.example.org"}, self.config)
        servers = server_list(load_config(self.config))
        self.assertTrue(any(server["endpoint"] == "https://lws.example.org"
                            and server["custom"] for server in servers))

    def test_default_xmr_endpoint_is_not_duplicated(self):
        add_address({"chain": "xmr", "address": XMR_ADDRESS,
                     "viewKey": XMR_VIEW_KEY}, self.config)
        servers = [server for server in server_list(load_config(self.config))
                   if server["endpoint"] == "https://lws.monero.fail"]
        self.assertEqual(len(servers), 1)


class UpdateTests(TempFiles):
    def add_xmr(self):
        return add_address({"chain": "xmr", "address": XMR_ADDRESS,
                            "viewKey": XMR_VIEW_KEY, "label": "Old"}, self.config)

    def test_label_update_preserves_view_key(self):
        entry = self.add_xmr()
        previous, updated = update_address(entry["id"], {"label": "New"}, self.config)
        self.assertEqual(previous["label"], "Old")
        self.assertEqual(updated["label"], "New")
        self.assertEqual(updated["viewKey"], XMR_VIEW_KEY)
        self.assertEqual(load_config(self.config)["addresses"][0]["label"], "New")

    def test_empty_label_clears_it(self):
        entry = self.add_xmr()
        _, updated = update_address(entry["id"], {"label": ""}, self.config)
        self.assertEqual(updated["label"], "")

    def test_address_change_needs_a_new_view_key(self):
        entry = self.add_xmr()
        other = "4" + "B" * 94
        with self.assertRaises(ValidationError):
            update_address(entry["id"], {"address": other}, self.config)

    def test_mode_change_uses_the_rpc_default(self):
        entry = self.add_xmr()
        _, updated = update_address(entry["id"], {"mode": "wallet-rpc"}, self.config)
        self.assertEqual(updated["endpoint"], "http://127.0.0.1:18083/json_rpc")
        self.assertEqual(updated["mode"], "wallet-rpc")

    def test_chain_change_does_not_keep_the_old_endpoint(self):
        entry = self.add_xmr()
        _, updated = update_address(entry["id"], {
            "chain": "btc",
            "address": BTC_ADDRESS,
        }, self.config)
        self.assertTrue(updated["endpoint"].startswith("https://mempool.space"))
        self.assertNotIn("viewKey", updated)
        self.assertNotIn("mode", updated)

    def test_endpoint_update_is_remembered(self):
        entry = self.add_xmr()
        _, updated = update_address(entry["id"], {"endpoint": "https://lws.example.org"},
                                    self.config)
        self.assertEqual(updated["endpoint"], "https://lws.example.org")
        self.assertTrue(any(server["endpoint"] == "https://lws.example.org"
                            for server in server_list(load_config(self.config))))
        self.assertEqual(updated["viewKey"], XMR_VIEW_KEY)

    def test_endpoint_change_drops_old_provider_credentials(self):
        add_address({
            "chain": "xmr",
            "address": XMR_ADDRESS,
            "mode": "wallet-rpc",
            "endpoint": "http://127.0.0.1:18083/json_rpc",
            "authUser": "alice",
            "authPass": "secret",
        }, self.config)
        entry = load_config(self.config)["addresses"][0]
        _, updated = update_address(entry["id"], {
            "endpoint": "http://127.0.0.1:18084/json_rpc",
        }, self.config)
        self.assertNotIn("authUser", updated)
        self.assertNotIn("authPass", updated)

    def test_unknown_watch_is_refused(self):
        with self.assertRaises(BlockwatchError):
            update_address("btc:nope", {"label": "x"}, self.config)

    def test_lightning_label_update_preserves_the_api_key(self):
        entry = add_address({"chain": "lightning", "address": LIGHTNING_ADDRESS,
                             "apiKey": BLINK_API_KEY, "label": "Old"}, self.config)
        _, updated = update_address(entry["id"], {"label": "New"}, self.config)
        self.assertEqual(updated["apiKey"], BLINK_API_KEY)
        self.assertEqual(updated["label"], "New")

    def test_lightning_address_change_needs_a_new_api_key(self):
        entry = add_address({"chain": "lightning", "address": LIGHTNING_ADDRESS,
                             "apiKey": BLINK_API_KEY}, self.config)
        other = "someoneelse@blink.sv"
        with self.assertRaises(ValidationError):
            update_address(entry["id"], {"address": other}, self.config)
        _, updated = update_address(entry["id"], {"address": other,
                                                  "apiKey": BLINK_API_KEY + "_new"},
                                    self.config)
        self.assertEqual(updated["address"], other)
        self.assertEqual(updated["apiKey"], BLINK_API_KEY + "_new")


class ConfigLockTests(TempFiles):
    def test_mutation_waits_for_the_config_lock(self):
        add_address({"chain": "btc", "address": BTC_ADDRESS}, self.config)
        cm = config_lock(self.config)
        cm.__enter__()
        done = []
        thread = threading.Thread(target=lambda: (
            add_address({"chain": "ltc", "address":
                         "LdP8Qox1VAhCzLJNqrr74YovaWYyNBUWvL"}, self.config),
            done.append(True),
        ))
        thread.start()
        time.sleep(0.2)
        self.assertTrue(thread.is_alive())
        cm.__exit__(None, None, None)
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(done, [True])
        self.assertEqual(len(load_config(self.config)["addresses"]), 2)


class RatesTests(unittest.TestCase):
    def test_fiat_formatting(self):
        report = {
            "currency": "USD", "symbol": "$", "decimals": 2,
            "values": {"BTC": "100000"},
        }
        fields = rates.fiat_fields("50000000", 8, "BTC", report)
        self.assertEqual(fields["fiatValue"], "50000.00")
        self.assertEqual(fields["fiatSymbol"], "$")
        self.assertEqual(fields["fiatDecimals"], 2)

    def test_missing_rate_leaves_value_empty(self):
        fields = rates.fiat_fields("100", 8, "BTC", {"currency": "USD",
                                                     "symbol": "$", "values": {}})
        self.assertIsNone(fields["fiatValue"])

    def test_fetch_rates_parses_one_document(self):
        document = {
            "bitcoin": {"usd": 95000.5},
            "monero": {"usd": 320.25},
        }
        with mock.patch("blockwatch.rates.http.request_json", return_value=document):
            values = rates.fetch_rates("usd")
        self.assertEqual(values["BTC"], "95000.5")
        self.assertEqual(values["XMR"], "320.25")
        self.assertNotIn("ETH", values)

    def test_fresh_cache_is_used_without_fetch(self):
        state = {"rates": {"currency": "USD", "fetchedAt": rates._now(),
                           "values": {"BTC": "100000"}}}
        with mock.patch("blockwatch.rates.fetch_rates") as fetch:
            report = rates.rates_for(state, "USD", fetch=True)
        fetch.assert_not_called()
        self.assertEqual(report["values"]["BTC"], "100000")
        self.assertFalse(report["stale"])

    def test_failed_fetch_falls_back_to_stale_cache(self):
        state = {"rates": {"currency": "USD", "fetchedAt": "2020-01-01T00:00:00Z",
                           "values": {"BTC": "100000"}}}
        with mock.patch("blockwatch.rates.fetch_rates",
                        side_effect=BlockwatchError("rate provider down")):
            report = rates.rates_for(state, "USD", fetch=True)
        self.assertTrue(report["stale"])
        self.assertEqual(report["values"]["BTC"], "100000")
        self.assertIn("rate provider down", report["error"])

    def test_cache_for_another_currency_is_not_reused(self):
        state = {"rates": {"currency": "USD", "fetchedAt": rates._now(),
                           "values": {"BTC": "100000"}}}
        with mock.patch("blockwatch.rates.fetch_rates",
                        side_effect=BlockwatchError("down")):
            report = rates.rates_for(state, "EUR", fetch=True)
        self.assertEqual(report["values"], {})
        self.assertEqual(report["currency"], "EUR")


class CliCredentialTests(TempFiles):
    def test_update_does_not_reapply_env_api_key_to_a_new_endpoint(self):
        add_address({
            "chain": "sol",
            "address": SOL_ADDRESS,
            "apiKey": "old-provider-token",
        }, self.config)
        entry = load_config(self.config)["addresses"][0]
        with mock.patch.dict(os.environ, {"BLOCKWATCH_API_KEY": "old-provider-token"}):
            with contextlib.redirect_stdout(io.StringIO()):
                code = cli.main([
                    "--config", self.config, "--state", self.state,
                    "update", entry["id"], "--endpoint", "https://solana.example.org",
                ])
        self.assertEqual(code, 0)
        updated = load_config(self.config)["addresses"][0]
        self.assertEqual(updated["endpoint"], "https://solana.example.org")
        self.assertNotIn("apiKey", updated)

    def test_update_still_accepts_an_explicit_new_api_key(self):
        add_address({
            "chain": "sol",
            "address": SOL_ADDRESS,
            "apiKey": "old-provider-token",
        }, self.config)
        entry = load_config(self.config)["addresses"][0]
        with contextlib.redirect_stdout(io.StringIO()):
            code = cli.main([
                "--config", self.config, "--state", self.state,
                "update", entry["id"], "--endpoint", "https://solana.example.org",
                "--api-key", "new-provider-token",
            ])
        self.assertEqual(code, 0)
        updated = load_config(self.config)["addresses"][0]
        self.assertEqual(updated["apiKey"], "new-provider-token")

    def test_update_replaces_a_key_from_the_environment_only_on_request(self):
        add_address({"chain": "lightning", "address": LIGHTNING_ADDRESS,
                     "apiKey": BLINK_API_KEY}, self.config)
        entry = load_config(self.config)["addresses"][0]
        with mock.patch.dict(os.environ, {"BLOCKWATCH_API_KEY": "blink_new_key"}):
            with contextlib.redirect_stdout(io.StringIO()):
                code = cli.main([
                    "--config", self.config, "--state", self.state,
                    "update", entry["id"], "--label", "Renamed",
                ])
            self.assertEqual(code, 0)
            self.assertEqual(load_config(self.config)["addresses"][0]["apiKey"],
                             BLINK_API_KEY)
            with contextlib.redirect_stdout(io.StringIO()):
                code = cli.main([
                    "--config", self.config, "--state", self.state,
                    "update", entry["id"], "--label", "Renamed again",
                    "--api-key-from-env",
                ])
            self.assertEqual(code, 0)
        self.assertEqual(load_config(self.config)["addresses"][0]["apiKey"],
                         "blink_new_key")


class ServerStatusTests(TempFiles):
    def test_status_carries_servers_and_safe_address_fields(self):
        add_address({"chain": "xmr", "address": XMR_ADDRESS,
                     "viewKey": XMR_VIEW_KEY}, self.config)
        result = watch.status(self.config, self.state)
        self.assertTrue(any(server["label"] == "lws.monero.fail"
                            for server in result["servers"]))
        address = result["addresses"][0]
        self.assertTrue(address["hasViewKey"])
        self.assertEqual(address["endpoint"], "https://lws.monero.fail")
        self.assertNotIn(XMR_VIEW_KEY, json.dumps(result))


class StatePruneTests(TempFiles):
    def setUp(self):
        super().setUp()
        patcher = mock.patch("blockwatch.rates.fetch_rates", return_value=FAKE_RATES)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_removed_address_state_is_pruned(self):
        add_address({"chain": "btc", "address": BTC_ADDRESS}, self.config)
        with mock.patch("blockwatch.watch.fetch",
                        return_value=Snapshot(balance_raw="1")):
            watch.poll(self.config, self.state)
        with open(self.state, encoding="utf-8") as handle:
            self.assertEqual(len(json.load(handle)["addresses"]), 1)
        save_config({"version": 1, "addresses": []}, self.config)
        watch.status(self.config, self.state)
        with open(self.state, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["addresses"], {})


if __name__ == "__main__":
    unittest.main()
