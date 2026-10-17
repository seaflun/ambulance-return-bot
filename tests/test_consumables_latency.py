import copy
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

from selenium.common.exceptions import UnexpectedAlertPresentException

import consumables_login as module
from ambulance_bot.models import AmbulanceReturnRequest


def ready_state(**changes):
    state = {
        "currentUrl": "https://nfaemsap3.nfa.gov.tw/ACS/ACS15002?emmTemsisid=synthetic01",
        "isSsoPage": False, "isDetailPage": True,
        "hasEmmTemsisidField": True, "emmTemsisid": "synthetic01",
        "hasCaseIdField": True, "caseId": "synthetic-case",
        "hasCallNoField": True, "callNo": "synthetic-car",
        "documentReady": "complete", "ajaxActive": 0,
    }
    state.update(changes)
    return state


class SnapshotDriver:
    def __init__(self, states):
        self.states = states
        self.execute_script = Mock(side_effect=lambda source: self.state)

    @property
    def current_url(self):
        raise AssertionError("Detail readiness must use the URL in its single snapshot")

    def find_elements(self, *args):
        raise AssertionError("Optional SSO markers must not incur an element lookup")


class SnapshotWait:
    def __init__(self, driver):
        self.driver = driver

    def until(self, predicate):
        for state in self.driver.states:
            self.driver.state = state
            if predicate(self.driver):
                return True
        raise module.TimeoutException("not ready")


class ConsumableDetailLatencyTests(unittest.TestCase):
    def test_ready_detail_uses_one_snapshot_without_repeated_url_or_missing_element_calls(self):
        driver = SnapshotDriver([ready_state()])
        self.assertTrue(module._wait_for_consumable_detail_page(driver, SnapshotWait(driver)))
        driver.execute_script.assert_called_once()

    def test_ajax_and_document_pending_each_require_a_fresh_snapshot(self):
        driver = SnapshotDriver([ready_state(documentReady="loading"), ready_state(ajaxActive=1), ready_state()])
        self.assertTrue(module._wait_for_consumable_detail_page(driver, SnapshotWait(driver)))
        self.assertEqual(driver.execute_script.call_count, 3)

    def test_sso_url_or_captcha_marker_never_reports_ready_detail(self):
        for state in (ready_state(isSsoPage=True), ready_state(isSsoPage=True, isDetailPage=False)):
            with self.subTest(state=state):
                driver = SnapshotDriver([state])
                self.assertFalse(module._wait_for_consumable_detail_page(driver, SnapshotWait(driver)))
                driver.execute_script.assert_called_once()

    def test_identity_missing_wrong_sid_and_non_detail_page_stay_unready(self):
        for change in ({"emmTemsisid": "other01"}, {"hasCaseIdField": False}, {"caseId": ""},
                       {"hasCallNoField": False}, {"callNo": ""}, {"isDetailPage": False}, {"ajaxActive": None}):
            with self.subTest(change=change):
                driver = SnapshotDriver([ready_state(**change)])
                self.assertFalse(module._wait_for_consumable_detail_page(driver, SnapshotWait(driver)))

    def test_direct_payload_check_uses_same_single_snapshot_identity_contract(self):
        driver = SnapshotDriver([])
        driver.state = ready_state()
        self.assertTrue(module._consumable_detail_payload_ready(driver))
        driver.execute_script.assert_called_once()


class ConsumableSaveLatencyTests(unittest.TestCase):
    def save(self, alert):
        driver = Mock()
        wait = Mock()
        wait.until.return_value = object()
        with patch.object(module, "_accept_alert_if_present", return_value=alert) as accept, patch.object(
            module.time, "sleep"
        ) as sleep:
            result = module._save_consumables(driver, wait)
        accept.assert_called_once_with(driver, timeout_seconds=5)
        return result, sleep, wait, driver

    def test_exact_accepted_native_success_skips_only_fixed_post_alert_sleep(self):
        for alert in ("儲存成功", "儲存成功。", " 存檔完成！ "):
            with self.subTest(alert=alert):
                result, sleep, wait, driver = self.save(alert)
                self.assertEqual(result, alert)
                sleep.assert_not_called()
                self.assertEqual(wait.until.call_count, 2)
                self.assertEqual(sum("button.click()" in str(call) for call in driver.execute_script.call_args_list), 1)

    def test_empty_unknown_question_or_success_mixed_with_other_text_keeps_old_observation(self):
        for alert in ("", "資料處理中", "儲存成功？", "儲存成功，請確認", "尚未儲存成功", "成功前請確認"):
            with self.subTest(alert=alert):
                _, sleep, _, _ = self.save(alert)
                sleep.assert_called_once_with(1.0)

    def test_failure_alert_stops_before_any_post_alert_delay(self):
        with patch.object(module, "_accept_alert_if_present", return_value="儲存失敗：錯誤"), patch.object(
            module.time, "sleep"
        ) as sleep:
            with self.assertRaisesRegex(RuntimeError, "耗材儲存失敗"):
                module._save_consumables(Mock(), Mock())
        sleep.assert_not_called()

    def test_success_alert_still_requires_same_sid_formal_get_and_exact_server_payload(self):
        request = AmbulanceReturnRequest(
            task_id="latency-check", created_at=datetime(2026, 10, 17, 10), raw_text="",
            consumables={"桃-9吋手套-L(雙)": 2},
        )
        expected = [{"itemId": "item-1", "quantity": "2", "name": "桃-9吋手套-L(雙)"}]
        url = "https://nfaemsap3.nfa.gov.tw/ACS/ACS15002?emmTemsisid=synthetic01"
        for actual, error in ((expected, None), ([], "讀回不一致")):
            with self.subTest(actual=actual):
                driver = Mock(current_url=url)
                driver.execute_script.return_value = "complete"
                wait = SimpleNamespace(until=lambda predicate: predicate(driver))
                with patch.dict("os.environ", {"SAVE_CONSUMABLES_RECORD": "true", "AUTOMATION_DRY_RUN": "false"}), patch.object(
                    module, "_resolve_consumable_item_quantities", return_value=copy.deepcopy(expected)
                ), patch.object(module, "_clear_existing_consumables"), patch.object(module, "_inject_consumables_for_save"), patch.object(
                    module, "_assert_consumable_rows_match"
                ), patch.object(module, "_accept_alert_if_present", return_value="儲存成功"), patch.object(
                    module, "_is_sso_page", return_value=False
                ), patch.object(module, "_wait_for_consumable_detail_page", return_value=True), patch.object(
                    module, "_read_consumable_payload_rows", return_value=actual
                ), patch.object(module.time, "sleep"):
                    if error:
                        with self.assertRaisesRegex(RuntimeError, error):
                            module._write_current_consumable_page(driver, wait, request)
                    else:
                        self.assertIn("按下儲存並確認", module._write_current_consumable_page(driver, wait, request))
                driver.get.assert_called_once_with(url)

    def test_delayed_error_alert_blocking_formal_get_is_not_reported_as_saved(self):
        request = AmbulanceReturnRequest(task_id="late-error", created_at=datetime.now(), raw_text="", consumables={"item": 1})
        driver = Mock(current_url="https://nfaemsap3.nfa.gov.tw/ACS/ACS15002?emmTemsisid=synthetic01")
        driver.execute_script.return_value = "complete"
        driver.get.side_effect = UnexpectedAlertPresentException("儲存後延遲錯誤")
        wait = SimpleNamespace(until=lambda predicate: predicate(driver))
        with patch.dict("os.environ", {"SAVE_CONSUMABLES_RECORD": "true", "AUTOMATION_DRY_RUN": "false"}), patch.object(
            module, "_resolve_consumable_item_quantities", return_value=[{"itemId": "1", "quantity": "1"}]
        ), patch.object(module, "_clear_existing_consumables"), patch.object(module, "_inject_consumables_for_save"), patch.object(
            module, "_assert_consumable_rows_match"
        ), patch.object(module, "_accept_alert_if_present", return_value="儲存成功"), patch.object(
            module, "_is_sso_page", return_value=False
        ), patch.object(module, "_verify_saved_consumables") as verify, patch.object(module.time, "sleep"):
            with self.assertRaises(UnexpectedAlertPresentException):
                module._write_current_consumable_page(driver, wait, request)
        verify.assert_not_called()
        driver.get.assert_called_once()

    def test_two_patients_each_click_once_and_reopen_their_own_sid_after_native_success(self):
        request = AmbulanceReturnRequest(
            task_id="two-fast-patients", created_at=datetime(2026, 10, 17, 10), raw_text="",
            case_id="20261017100123001", vehicle="新坡92", consumables={"item": 3},
        )
        hrefs = ["/ACS/ACS15002?emmTemsisid=20261017101003100123" + suffix for suffix in ("01", "02")]
        driver = Mock(current_url=module.ACS_URL)
        driver.get.side_effect = lambda url: setattr(driver, "current_url", url)
        injected, saved = {}, []

        def execute(source, *args):
            if "button.click()" in source:
                saved.append(driver.current_url)
            return "complete"

        driver.execute_script.side_effect = execute
        wait = SimpleNamespace(until=lambda predicate: predicate(driver))
        with patch.dict("os.environ", {"SAVE_CONSUMABLES_RECORD": "true", "AUTOMATION_DRY_RUN": "false"}), patch.object(
            module, "WebDriverWait", return_value=wait
        ), patch.object(module, "_open_consumable_maintenance_page"), patch.object(module, "_find_consumable_detail_hrefs", return_value=hrefs), patch.object(
            module, "_wait_for_consumable_detail_page", return_value=True
        ), patch.object(module, "_consumable_detail_case_id", return_value=request.case_id), patch.object(
            module, "_consumable_detail_vehicle_label", return_value=request.vehicle
        ), patch.object(module, "_resolve_consumable_item_quantities", side_effect=lambda d, r, **kw: [
            {"itemId": "item-1", "quantity": str(r.consumables["item"])}
        ]), patch.object(module, "_clear_existing_consumables"), patch.object(
            module, "_inject_consumables_for_save", side_effect=lambda d, items: injected.update({d.current_url: items})
        ), patch.object(module, "_assert_consumable_rows_match"), patch.object(module, "_is_sso_page", return_value=False), patch.object(
            module, "_accept_alert_if_present", return_value="儲存成功"
        ) as accept, patch.object(module, "_read_consumable_payload_rows", side_effect=lambda d: injected[d.current_url]), patch.object(
            module.time, "sleep"
        ) as sleep:
            result = module._open_consumable_record_for_task(driver, request)
        self.assertIn("兩頁均已儲存確認", result)
        self.assertEqual(saved, ["https://nfaemsap3.nfa.gov.tw" + href for href in hrefs])
        self.assertEqual([c.args[0] for c in driver.get.call_args_list], [saved[0], saved[0], saved[1], saved[1]])
        self.assertEqual([injected[url][0]["quantity"] for url in saved], ["2", "1"])
        self.assertEqual(accept.call_count, 2)
        sleep.assert_not_called()
