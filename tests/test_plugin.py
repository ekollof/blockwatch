"""Static checks for the plugin surface: manifest, entry points and strings."""

import json
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
        return json.load(handle)


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.manifest = load("manifest.json")

    def test_identity_and_kinds(self):
        self.assertEqual(self.manifest["id"], "io.github.ekollof.blockwatch")
        self.assertEqual(self.manifest["schemaVersion"], 1)
        self.assertIn("bar-widget", self.manifest["kinds"])
        self.assertIn("service", self.manifest["kinds"])
        self.assertTrue(self.manifest["keepLoaded"])

    def test_entry_points_exist(self):
        for kind, relative in self.manifest["entryPoints"].items():
            path = os.path.join(ROOT, relative)
            self.assertTrue(os.path.isfile(path), f"{kind} entry point is missing: {relative}")

    def test_bar_widget_schema(self):
        widget = self.manifest["barWidget"]
        self.assertFalse(widget["allowMultiple"])
        self.assertEqual(widget["defaultSection"], "right")
        keys = {item["key"] for item in widget["schema"]}
        self.assertEqual(keys, {"refreshIntervalSec", "notify", "fiatCurrency"})
        for item in widget["schema"]:
            if item["type"] == "integer":
                self.assertLessEqual(item["min"], item["defaultValue"])
                self.assertGreaterEqual(item["max"], item["defaultValue"])
            elif item["type"] == "boolean":
                self.assertTrue(item["defaultValue"] in (True, False))
            else:
                self.assertEqual(item["type"], "string")
                self.assertTrue(item["defaultValue"])

    def test_omakit_run_block_is_vendored(self):
        for relative in ("omakit/Run.qml", "omakit/run-supervisor.py", "omakit/NOTICE"):
            self.assertTrue(os.path.isfile(os.path.join(ROOT, relative)), relative)

    def test_helper_runs_isolated(self):
        with open(os.path.join(ROOT, "service.qml"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn('"/usr/bin/python3", "-I", "-S", "-B"', source)

    def test_panel_does_not_put_secrets_in_argv(self):
        with open(os.path.join(ROOT, "service.qml"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertNotIn('"--view-key"', source)
        self.assertNotIn('"--api-key"', source)
        self.assertNotIn('"--rpc-password"', source)
        self.assertIn("BLOCKWATCH_VIEW_KEY", source)
        self.assertIn("BLOCKWATCH_API_KEY", source)

    def test_notification_text_neutralizes_markup(self):
        with open(os.path.join(ROOT, "service.qml"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("notificationText", source)
        self.assertIn("replace(/[<>&]/g", source)

    def test_config_mutations_cancel_an_in_flight_poll(self):
        with open(os.path.join(ROOT, "service.qml"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("queueMutation", source)
        self.assertIn("pollRun.cancel()", source)
        self.assertIn("pendingMutation", source)

    def test_service_passes_the_fiat_currency(self):
        with open(os.path.join(ROOT, "service.qml"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn('"--fiat"', source)
        self.assertIn("fiatCurrency", source)


class TranslationTests(unittest.TestCase):
    def test_every_used_key_exists(self):
        catalog = load("i18n/en.json")
        used = set()
        pattern = re.compile(r'strings\.tn?\("([^"]+)"')
        for relative in ("BarWidget.qml", "AddressRow.qml", "service.qml"):
            with open(os.path.join(ROOT, relative), encoding="utf-8") as handle:
                used.update(key for key in pattern.findall(handle.read()) if not key.endswith("."))
        self.assertTrue(used)
        missing = sorted(key for key in used if key not in catalog)
        self.assertEqual(missing, [])

    def test_plural_entries_have_one_and_other(self):
        for key, value in load("i18n/en.json").items():
            if isinstance(value, dict):
                self.assertIn("one", value, key)
                self.assertIn("other", value, key)

    def test_relative_time_units_exist(self):
        catalog = load("i18n/en.json")
        for unit in ("now", "minutes", "hours", "days"):
            self.assertIn("time." + unit, catalog)


if __name__ == "__main__":
    unittest.main()
