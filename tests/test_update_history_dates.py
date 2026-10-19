import copy
from datetime import datetime
import html
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest import mock
import urllib.error

import app as app_module


RUNTIME = Path(__file__).resolve().parents[1] / "WinPython_公務電腦使用包"


def update_cards(body):
    cards = []
    for attributes, content in re.findall(r"<article\b([^>]*)>(.*?)</article>", body, re.S):
        period = re.search(r'class="update-card__period">(.*?)</p>', content, re.S)
        if period is None:
            continue
        version = re.search(r'data-(?:duty-)?release-version="([^"]+)"', attributes)
        cards.append({"version": version.group(1) if version else "",
                      "period": html.unescape(period.group(1)).strip()})
    return cards


def month_semantics(period):
    match = re.fullmatch(r"(\d{4})\s*(?:年|/)\s*(\d{1,2})\s*月?", period)
    return f"{match[1]}/{int(match[2]):02d}" if match else period


class UpdateHistoryDateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ambulance-update-history-")
        self.addCleanup(temporary.cleanup)
        patches = (
            mock.patch.dict(os.environ, {"TASK_ENTRY_HOSTS": "", "TASK_ENTRY_PORT": "",
                                         "TASK_ENTRY_URL": "", "PUBLIC_PC_REPORT_ENABLED": "false"}),
            mock.patch.object(app_module, "artifacts_dir", Path(temporary.name)),
            mock.patch.object(app_module, "_duty_updates_cache", {"expires_at": 0, "history": None}),
            mock.patch.dict(app_module.app.config, {"TESTING": True}),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.client = app_module.app.test_client()
        self.history_path = RUNTIME / "templates/sinposmart_update_history.json"
        self.history_bytes = self.history_path.read_bytes()
        self.history = json.loads(self.history_bytes.decode("utf-8-sig"))

    def get_page(self, system):
        response = self.client.get(f"/updates?system={system}", headers={"Host": "100.114.126.58:8080"})
        self.assertEqual(200, response.status_code)
        return response.get_data(as_text=True)

    def assert_release_periods(self, cards):
        releases = [card for card in cards if card["version"]]
        self.assertTrue(releases, "The HTTP response did not contain versioned update cards")
        for card in releases:
            with self.subTest(version=card["version"]):
                expected = datetime.strptime(card["version"], "%Y.%m.%d.%H%M").strftime("%Y/%m/%d %H:%M")
                self.assertEqual(expected, card["period"])

    def test_both_tabs_render_each_version_as_taipei_date_and_minute(self):
        with mock.patch.object(app_module, "_read_text_url", side_effect=urllib.error.URLError("offline")):
            for system in ("ambulance", "duty"):
                with self.subTest(system=system):
                    self.assert_release_periods(update_cards(self.get_page(system)))

    def test_formatter_preserves_taipei_build_time_at_midnight_and_leap_day(self):
        formatter = getattr(app_module, "update_release_datetime", None)
        self.assertTrue(callable(formatter), "Expose update_release_datetime to the update-page renderer")
        for version, expected in (
            ("2026.12.31.2355", "2026/12/31 23:55"),
            ("2027.01.01.0005", "2027/01/01 00:05"),
            ("2028.02.29.0000", "2028/02/29 00:00"),
        ):
            with self.subTest(version=version):
                self.assertEqual(expected, formatter(version))

    def test_formatter_rejects_invalid_versions_and_preserves_fallback(self):
        formatter = getattr(app_module, "update_release_datetime", None)
        self.assertTrue(callable(formatter), "Expose update_release_datetime to the update-page renderer")
        for version in (None, 123, "", "2026.1.02.0304", "2026.02.30.1200",
                        "2026.10.19.2400", "2026.10.19.2043Z", "2026/10/19 20:43"):
            with self.subTest(version=version):
                self.assertEqual("日期待確認", formatter(version, "日期待確認"))
                self.assertEqual("", formatter(version))

    def test_duty_ssr_keeps_fallback_when_an_entry_has_no_valid_version(self):
        history = {"source_unavailable": False, "major_history": [], "releases": [
            {"version": "not-a-version", "period": "日期待確認", "title": "既有說明", "items": ["保留內容"]},
        ]}
        with mock.patch.object(app_module, "duty_update_history", return_value=history):
            cards = update_cards(self.get_page("duty"))
        self.assertEqual([{"version": "not-a-version", "period": "日期待確認"}], cards)

    def test_remote_history_and_offline_cache_keep_the_same_midnight_dates(self):
        remote = {"major_history": [{"period": "初期建置", "title": "歷史里程碑", "items": ["原說明"]}],
                  "releases": [
                      {"version": "2027.01.01.0005", "period": "2027/01/01", "title": "午夜新版", "items": ["跨年"]},
                      {"version": "2026.12.31.2355", "period": "2026/12/31", "title": "午夜前版", "items": ["原說明"]},
                  ]}
        original = copy.deepcopy(remote)
        with mock.patch.object(app_module.time, "monotonic", return_value=42) as clock, \
                mock.patch.object(app_module, "_read_text_url", side_effect=[remote["releases"][0]["version"], json.dumps(remote)]) as reader:
            online = self.get_page("duty")
            self.assertNotIn("已保存的更新歷程", online)
            self.assert_release_periods(update_cards(online))
            self.assertLess(online.index("2027.01.01.0005"), online.index("2026.12.31.2355"))
            self.assertEqual(2, reader.call_count)
            clock.return_value = 400
            reader.side_effect = urllib.error.URLError("offline")
            offline = self.get_page("duty")
            self.assertIn("已保存的更新歷程", offline)
            self.assertEqual(update_cards(online), update_cards(offline))
        self.assertEqual(original, remote)
        self.assertEqual(self.history_bytes, self.history_path.read_bytes())

    def test_monthly_milestones_keep_month_precision_without_invented_day_or_time(self):
        with mock.patch.object(app_module, "_read_text_url", side_effect=urllib.error.URLError("offline")):
            duty = [card["period"] for card in update_cards(self.get_page("duty")) if not card["version"]]
            rescue = [card["period"] for card in update_cards(self.get_page("ambulance")) if not card["version"]]
        self.assertEqual([month_semantics(entry["period"]) for entry in self.history["major_history"]],
                         [month_semantics(period) for period in duty])
        self.assertEqual(["2026/09", "2026/08", "2026/07", "2026/06"],
                         [month_semantics(period) for period in rescue])
        for period in duty + rescue:
            self.assertNotRegex(period, r"\d{2}:\d{2}|\d{4}/\d{2}/\d{2}")


if __name__ == "__main__":
    unittest.main()
