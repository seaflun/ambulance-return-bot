import copy
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import app as web
from ambulance_bot import desktop_fast_runner
from ambulance_bot.models import AmbulanceReturnRequest
from ambulance_bot.task_store import JsonTaskStore


class MileageStageDisplayTests(unittest.TestCase):
    def report(self, detail):
        return {"site_statuses": {"vehicle_mileage": {"status": "vehicle_mileage_saved"}},
                "events": [{"event_id": "historical-progress", "time": "2026-10-15T00:06:45",
                            "action": "公務電腦回報車輛里程", "status": "vehicle_mileage_running",
                            "detail": detail}]}

    def test_historical_mileage_query_is_classified_without_changing_saved_records(self):
        report = self.report("登入帳號：里程=任務司機 > 出勤人員 > 同步帳號，任務司機，"
                             "27番 測試司機 - tyfd02321。公務電腦 worker 車輛里程：依案件時間查詢前後里程")
        before = copy.deepcopy(report)
        row = web.public_pc_event_rows(report)[0]
        self.assertEqual("里程｜填寫里程｜執行中", row["action"])
        self.assertEqual("填寫里程", row["stage_group"])
        self.assertEqual(before, report)

    def test_desktop_readback_progress_is_grouped_as_confirmation(self):
        row = web.public_pc_event_rows(self.report("本機快速 車輛里程：本案與後一筆里程已核對"))[0]
        self.assertEqual("里程｜儲存確認｜執行中", row["action"])

    def test_unrecognized_historical_progress_does_not_borrow_latest_success(self):
        row = web.public_pc_event_rows(self.report("公務電腦 worker 車輛里程：尚無可識別的步驟"))[0]
        self.assertEqual("里程｜執行中", row["action"])
        self.assertEqual("vehicle_mileage_running", row["status"])

    def test_desktop_emits_mileage_group_transitions_for_the_actual_progress_messages(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(desktop_fast_runner, "login_audit_for_site", return_value=""):
            store = JsonTaskStore(Path(tmp) / "tasks")
            task = AmbulanceReturnRequest(task_id="mileage-phases", created_at=datetime.now(), raw_text="")
            store.create(task)
            reports = []
            runner = desktop_fast_runner.DesktopFastRunner(
                Path(tmp), store=store, event_callback=lambda payload, action: reports.append((payload, action)))
            owner = runner._prepare_execution(task.task_id, task.task_id, "busy")
            self.assertTrue(owner)
            try:
                runner._report_site_progress(task.task_id, "vehicle_mileage", "依案件時間查詢前後里程")
                runner._report_site_progress(task.task_id, "vehicle_mileage", "本案與後一筆里程已核對")
            finally:
                runner._release_prepared_execution(task.task_id, owner, task.task_id)
            self.assertEqual(["車輛里程 階段：填寫里程", "車輛里程 階段：儲存確認"],
                             [action for _, action in reports])


if __name__ == "__main__":
    unittest.main()
