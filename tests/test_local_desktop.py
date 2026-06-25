import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from ambulance_bot.local_desktop import local_browser_enabled, open_task_on_local_desktop
from ambulance_bot.models import AmbulanceReturnRequest, FuelRecord


class LocalDesktopTests(unittest.TestCase):
    def test_local_browser_enabled_default_and_false(self):
        os.environ.pop("OPEN_LOCAL_BROWSER_ON_RUN", None)
        self.assertTrue(local_browser_enabled())
        os.environ["OPEN_LOCAL_BROWSER_ON_RUN"] = "false"
        self.assertFalse(local_browser_enabled())

    def test_open_task_writes_summary_and_opens_four_sites(self):
        request = AmbulanceReturnRequest(
            task_id="task-1",
            created_at=datetime.now(),
            raw_text="",
            vehicle="91A1",
        )
        with tempfile.TemporaryDirectory() as tmp:
            with patch("webbrowser.open_new_tab") as open_tab:
                os.environ["BROWSER_OPEN_DELAY_SECONDS"] = "0"
                path = open_task_on_local_desktop(request, Path(tmp))
                self.assertTrue(path.exists())
                summary = path.read_text(encoding="utf-8")

        self.assertEqual(open_tab.call_count, 4)
        opened_urls = [call.args[0] for call in open_tab.call_args_list]
        self.assertIn("dutymgt.tyfd.gov.tw", opened_urls[0])
        self.assertIn("ppe.tyfd.gov.tw", opened_urls[1])
        self.assertIn("nfaemsap3.nfa.gov.tw", opened_urls[2])
        self.assertIn("emsdt.tyfd.gov.tw", opened_urls[3])
        self.assertLess(summary.index("消防勤務工作紀錄"), summary.index("車輛里程"))
        self.assertLess(summary.index("車輛里程"), summary.index("一站通耗材"))
        self.assertLess(summary.index("一站通耗材"), summary.index("緊急救護消毒"))


    def test_open_task_opens_fuel_site_when_fuel_record_is_enabled(self):
        request = AmbulanceReturnRequest(
            task_id="task-fuel",
            created_at=datetime.now(),
            raw_text="",
            vehicle="91A1",
            fuel_record=FuelRecord(enabled=True, date="2026/06/25", time="1720"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            with patch("webbrowser.open_new_tab") as open_tab:
                os.environ["BROWSER_OPEN_DELAY_SECONDS"] = "0"
                path = open_task_on_local_desktop(request, Path(tmp))
                summary = path.read_text(encoding="utf-8")

        self.assertEqual(open_tab.call_count, 5)
        opened_urls = [call.args[0] for call in open_tab.call_args_list]
        self.assertIn("FUC04100/Query", opened_urls[2])
        self.assertIn("5\u7ad9\u9023\u7d50", summary)


if __name__ == "__main__":
    unittest.main()
