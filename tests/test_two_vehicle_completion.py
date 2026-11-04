from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import consumables_login
import worker
from ambulance_bot import selenium_local
from ambulance_bot.models import AmbulanceReturnRequest


class TwoVehicleCompletionTests(unittest.TestCase):
    def test_consumables_retry_preserves_first_vehicle_and_labels_second_vehicle_progress(self):
        task = {
            "task_id": "synthetic-consumables-retry", "created_at": "2026-11-03T21:00:00",
            "service_type": "ems", "vehicle": "新坡95", "two_vehicle": True,
            "vehicle_entries": [{"vehicle": "新坡95"}, {"vehicle": "新坡92"}],
        }
        saved = {"新坡95": {"status": "consumables_saved", "detail": "已核對第一台"}}
        actions = []

        def action(_driver, request, **kwargs):
            actions.append(request.vehicle)
            kwargs["progress"]("回查耗材紀錄")
            return "已儲存並核對"

        with patch.object(worker, "login_audit_for_site", return_value=""), patch.object(
            worker, "login_acs_and_get_driver", return_value=object()
        ), patch.object(worker, "open_consumable_record_for_task", side_effect=action), patch.object(
            worker, "post_status"
        ) as post:
            result = worker.run_consumables_worker_task(
                "http://nas", "synthetic-worker", task, Path("artifacts"),
                vehicle_results=saved, update_overall=False,
            )
        self.assertEqual(actions, ["新坡92"])
        self.assertEqual(saved["新坡95"]["detail"], "已核對第一台")
        self.assertEqual(result.status, "consumables_saved")
        progress = [call for call in post.call_args_list if "回查耗材紀錄" in call.args[3]]
        self.assertEqual(len(progress), 1)
        self.assertEqual(progress[0].kwargs.get("vehicle_key"), "新坡92")

    def test_local_driver_commands_are_bounded_for_first_and_attached_vehicle(self):
        for attach in (False, True):
            with self.subTest(attach=attach), TemporaryDirectory() as tmp:
                driver = Mock(command_executor=SimpleNamespace(
                    _client_config=SimpleNamespace(timeout=None),
                ))
                with patch.dict(selenium_local.os.environ, {
                    "SELENIUM_REMOTE_URL": "", "SELENIUM_COMMAND_TIMEOUT_SECONDS": "0.05",
                    "SELENIUM_CHROME_START_ATTEMPTS": "1",
                }), patch.object(selenium_local.webdriver, "Chrome", return_value=driver), patch.object(
                    selenium_local, "_cleanup_stale_profiles_for_driver"
                ), patch.object(selenium_local, "_profile_dir", return_value=Path(tmp)), patch.object(
                    selenium_local, "schedule_driver_auto_close"
                ):
                    result = selenium_local._create_driver(
                        Path(tmp), debugger_port=9234, attach_existing=attach,
                    )
                self.assertIs(result, driver)
                self.assertEqual(driver.command_executor._client_config.timeout, 0.05)
                driver.set_page_load_timeout.assert_called_once()
                driver.set_script_timeout.assert_called_once()

    def test_optional_screenshots_cannot_hold_verified_mileage_for_a_full_command_timeout(self):
        timeouts = []
        config = SimpleNamespace(timeout=60)

        class Driver:
            command_executor = SimpleNamespace(_client_config=config)

            def save_screenshot(self, _path):
                timeouts.append(config.timeout)
                raise TimeoutError("renderer is stuck after verified save")

            @property
            def page_source(self):
                timeouts.append(config.timeout)
                raise TimeoutError("renderer is stuck after verified save")

        with TemporaryDirectory() as tmp:
            selenium_local._save_artifacts(Driver(), Path(tmp), "synthetic-task", "vehicle_mileage")
        self.assertEqual(len(timeouts), 2)
        self.assertTrue(all(0 < timeout <= 4 for timeout in timeouts), timeouts)
        self.assertEqual(config.timeout, 60)

    def test_verified_consumables_return_without_another_renderer_read(self):
        request = AmbulanceReturnRequest(
            task_id="synthetic-two-vehicle", created_at=datetime(2026, 11, 3), raw_text="",
            case_id="20261103195628001", vehicle="新坡92",
            consumables={"桃-9吋手套-L(雙)": 1},
        )
        href = "https://nfaemsap3.nfa.gov.tw/ACS/ACS15002?emmTemsisid=2026110310100319562802"

        class Driver:
            verified = False

            @property
            def current_url(self):
                if self.verified:
                    raise TimeoutError("renderer stalled after saved contents were verified")
                return href

        driver = Driver()

        def write_and_verify(*_args, **_kwargs):
            driver.verified = True
            return " 已按下儲存並確認。"

        with patch.object(consumables_login, "_consume_initial_consumable_list", return_value=True), patch.object(
            consumables_login, "_new_consumable_classification_context", return_value=None
        ), patch.object(consumables_login, "_find_consumable_detail_hrefs", return_value=[href]), patch.object(
            consumables_login, "_open_consumable_detail_if_needed"
        ), patch.object(consumables_login, "_wait_for_consumable_detail_page", return_value=True), patch.object(
            consumables_login, "_consumable_detail_case_id", return_value=request.case_id
        ), patch.object(consumables_login, "_consumable_detail_vehicle_label", return_value="新坡92"), patch.object(
            consumables_login, "_write_current_consumable_page", side_effect=write_and_verify
        ), patch.dict(consumables_login.os.environ, {"SAVE_CONSUMABLES_RECORD": "true"}):
            detail = consumables_login._open_consumable_record_for_task(driver, request)
        self.assertIn(href, detail)
        self.assertIn("儲存並確認", detail)
