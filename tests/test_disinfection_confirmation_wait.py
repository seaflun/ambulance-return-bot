import json
import shutil
import subprocess
import unittest
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from selenium.common.exceptions import NoAlertPresentException

from ambulance_bot import selenium_local as runtime
from ambulance_bot.models import AmbulanceReturnRequest
from ambulance_bot.task_cancellation import TaskCancellationError


class VirtualClock:
    def __init__(self):
        self.now = 0.0

    def sleep(self, seconds):
        self.now += seconds


class JitterClock(VirtualClock):
    def sleep(self, seconds):
        self.now += seconds + 0.001


class ConfirmationBrowser:
    def __init__(self, clock, prompts=(), navigation_at=None, login_at=None, url_change_at=None, stamp=True):
        self.clock = clock
        self.prompts = [dict(at=at, kind=kind, text=text, handled=False) for at, kind, text in prompts]
        self.navigation_at = navigation_at
        self.login_at = login_at
        self.url_change_at = url_change_at
        self.stamp = stamp
        self.save_clicks = 0
        self.handled = []
        self.switch_to = type("SwitchTo", (), {
            "default_content": lambda _switch: None,
            "alert": property(lambda _switch: self.native_alert()),
        })()
        self.get = Mock()

    @property
    def current_url(self):
        if self.login_at is not None and self.clock.now >= self.login_at:
            return "https://emsdt.tyfd.gov.tw/EmmWeb/login.jsp"
        suffix = "saved" if self.url_change_at is not None and self.clock.now >= self.url_change_at else "entry"
        return "https://emsdt.tyfd.gov.tw/EmmWeb/" + suffix

    @property
    def page_source(self):
        return ""

    def pending(self, kind):
        return next((p for p in self.prompts if p["kind"] == kind and not p["handled"]
                     and p["at"] <= self.clock.now + 1e-9), None)

    def handle(self, prompt):
        prompt["handled"] = True
        self.handled.append((prompt["kind"], prompt["text"], self.clock.now))

    def native_alert(self):
        prompt = self.pending("native")
        if prompt is None:
            raise NoAlertPresentException()
        return SimpleNamespace(text=prompt["text"], accept=lambda: self.handle(prompt))

    def execute_script(self, script, *args):
        if "target.click()" in script and "_btnSave" in script:
            self.save_clicks += 1
            return True
        origin = 2000.0 if self.navigation_at is not None and self.clock.now >= self.navigation_at else 1000.0
        if "performance.timeOrigin" in script and "swal-overlay" not in script:
            return origin if self.stamp else None
        if "swal-overlay" in script:
            prompt = self.pending("sweetalert")
            result = {"clicked": False, "text": "", "visible": False,
                      "origin": origin if self.stamp else None, "ready": True, "url": self.current_url}
            if prompt is not None:
                self.handle(prompt)
                result.update(clicked=True, text=prompt["text"], visible=True)
            return result
        raise AssertionError("unexpected browser operation")


class ScriptContractBrowser(ConfirmationBrowser):
    def __init__(self, clock, prompts=(), modal_until=3.0, enabled=True, has_button=True, ready_at=0.0,
                 text_changes=(), button_changes=(), hidden_ranges=(), **kwargs):
        super().__init__(clock, prompts, **kwargs)
        self.modal_until = modal_until
        self.enabled = enabled
        self.has_button = has_button
        self.ready_at = ready_at
        self.modal_confirmed = False
        self.modal_clicks = 0
        self.prompt_state = None
        self.text_changes = text_changes
        self.button_changes = button_changes
        self.hidden_ranges = hidden_ranges

    def execute_script(self, script, *args):
        if "swal-overlay" not in script:
            return super().execute_script(script, *args)
        payload = {
            "script": script, "visible": self.clock.now < self.modal_until
            and not any(start <= self.clock.now < end for start, end in self.hidden_ranges),
            "enabled": self.enabled, "has_button": self.has_button,
            "confirmed": self.modal_confirmed, "text": "儲存成功",
            "prompt_state": self.prompt_state, "button_id": "first",
            "ready": self.clock.now >= self.ready_at, "url": self.current_url,
            "origin": 2000.0 if self.navigation_at is not None and self.clock.now >= self.navigation_at else 1000.0,
        }
        for at, text in self.text_changes:
            if self.clock.now >= at:
                payload["text"] = text
        for at, button_id in self.button_changes:
            if self.clock.now >= at:
                payload["button_id"] = button_id
        harness = """
        const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
        let clicks = 0;
        const button = {disabled: !input.enabled, innerText: '確定', click: () => clicks++};
        const modal = {
          querySelectorAll: selector => selector === 'button'
            ? (input.has_button ? [button] : []) : [{innerText: input.text}],
          querySelector: () => input.has_button ? button : null
        };
        const overlay = {__ambulanceDisinfectionConfirmed: input.confirmed,
          querySelector: () => modal};
        global.document = {readyState: input.ready ? 'complete' : 'loading',
          querySelector: () => input.visible ? overlay : null};
        global.window = {performance: {timeOrigin: input.origin}};
        if (input.prompt_state) {
          window.__ambulanceDisinfectionSavePrompt = {overlay,
            target: input.prompt_state.button_id === input.button_id ? button : {},
            text: input.prompt_state.text};
        }
        global.location = {href: input.url};
        const value = new Function(input.script)();
        const prompt = window.__ambulanceDisinfectionSavePrompt;
        process.stdout.write(JSON.stringify({value, clicks,
          confirmed: overlay.__ambulanceDisinfectionConfirmed,
          prompt_state: prompt ? {text: prompt.text,
            button_id: prompt.target === button ? input.button_id : input.prompt_state.button_id} : null}));
        """
        result = subprocess.run(
            [shutil.which("node"), "-e", harness], input=json.dumps(payload),
            capture_output=True, text=True, encoding="utf-8", check=True,
        )
        output = json.loads(result.stdout)
        self.modal_confirmed = output["confirmed"]
        self.prompt_state = output["prompt_state"]
        self.modal_clicks += output["clicks"]
        return output["value"]


class DisinfectionConfirmationWaitTests(unittest.TestCase):
    def prepare(self, browser, clock, readback_error=None, cancel_at=None):
        request = AmbulanceReturnRequest(
            task_id="confirmation-test", created_at=datetime(2026, 10, 16), raw_text="",
            vehicle="test-vehicle", case_time="1200", disinfection_items=["救護車體"],
        )
        readback = Mock(side_effect=readback_error)

        def cancel():
            if cancel_at is not None and clock.now >= cancel_at:
                raise TaskCancellationError("cancelled")

        with ExitStack() as stack:
            for name in (
                "_switch_to_disinfection_content_if_present", "_wait_for_disinfection_query_fields",
                "_set_disinfection_query_date", "_wait_for_disinfection_query_completed",
                "_save_disinfection_progress_artifacts", "_wait_for_disinfection_detail_ready",
            ):
                stack.enter_context(patch.object(runtime, name))
            for name in ("_click_disinfection_query", "_open_disinfection_detail_for_case",
                         "_save_disinfection_record_enabled"):
                stack.enter_context(patch.object(runtime, name, return_value=True))
            stack.enter_context(patch.object(runtime, "_set_disinfection_item_statuses", return_value=1))
            stack.enter_context(patch.object(runtime, "_verify_saved_disinfection_record", readback))
            stack.enter_context(patch.object(runtime, "_assert_automation_write_allowed"))
            stack.enter_context(patch.object(runtime.time, "monotonic", side_effect=lambda: clock.now))
            stack.enter_context(patch.object(runtime.time, "time", side_effect=lambda: clock.now))
            stack.enter_context(patch.object(runtime.time, "sleep", side_effect=clock.sleep))
            try:
                detail = runtime._prepare_disinfection_record(
                    browser, request, Path("unused"), cancel_check=cancel,
                )
            except Exception:
                self.last_readback = readback
                raise
        self.last_readback = readback
        return runtime._confirmation_aware_status(
            "disinfection", detail, save_enabled=True, prefilled_status="disinfection_prefilled",
        )

    def test_cross_document_terminal_success_does_not_wait_for_absent_prompt_types(self):
        clock = VirtualClock()
        browser = ConfirmationBrowser(clock, [(1.0, "native", "儲存成功")], navigation_at=1.4)
        self.assertEqual(self.prepare(browser, clock), "disinfection_saved")
        self.assertLess(clock.now, 2.0)
        self.assertEqual(browser.save_clicks, 1)
        self.last_readback.assert_called_once()

    def test_delayed_native_success_is_handled_without_resubmitting(self):
        clock = VirtualClock()
        browser = ConfirmationBrowser(clock, [(6.4, "native", "儲存成功")], navigation_at=6.8)
        self.assertEqual(self.prepare(browser, clock), "disinfection_saved")
        self.assertEqual([p[:2] for p in browser.handled], [("native", "儲存成功")])
        self.assertLess(clock.now, 7.2)
        self.assertEqual(browser.save_clicks, 1)

    def test_sweetalert_then_delayed_native_failure_stops_before_readback(self):
        clock = VirtualClock()
        browser = ConfirmationBrowser(clock, [(1.0, "sweetalert", "請確認"), (1.6, "native", "儲存失敗")])
        with self.assertRaisesRegex(runtime.WebDriverException, "儲存失敗"):
            self.prepare(browser, clock)
        self.assertEqual([p[0] for p in browser.handled], ["sweetalert", "native"])
        self.assertLess(clock.now, 2.0)
        self.last_readback.assert_not_called()
        self.assertEqual(browser.save_clicks, 1)

    def test_unknown_then_success_requires_cross_document_before_early_readback(self):
        clock = VirtualClock()
        browser = ConfirmationBrowser(
            clock, [(1.0, "native", "權限狀態不明"), (1.4, "native", "存檔成功")], navigation_at=1.8,
        )
        self.assertEqual(self.prepare(browser, clock), "disinfection_saved")
        self.assertEqual(len(browser.handled), 2)
        self.assertLess(clock.now, 2.2)

    def test_success_in_old_document_does_not_hide_late_failure(self):
        clock = VirtualClock()
        browser = ConfirmationBrowser(clock, [(1.0, "native", "儲存成功"), (5.4, "native", "儲存失敗")])
        with self.assertRaisesRegex(runtime.WebDriverException, "儲存失敗"):
            self.prepare(browser, clock)
        self.last_readback.assert_not_called()
        self.assertEqual(len(browser.handled), 2)

    def test_native_success_does_not_hide_delayed_sweetalert_failure(self):
        clock = VirtualClock()
        browser = ConfirmationBrowser(clock, [(1.0, "native", "儲存成功"), (2.0, "sweetalert", "儲存失敗")])
        with self.assertRaisesRegex(runtime.WebDriverException, "儲存失敗"):
            self.prepare(browser, clock)
        self.last_readback.assert_not_called()
        self.assertEqual([p[0] for p in browser.handled], ["native", "sweetalert"])

    def test_url_change_without_document_change_does_not_end_observation(self):
        clock = VirtualClock()
        browser = ConfirmationBrowser(clock, [(1.0, "native", "儲存成功")], url_change_at=1.4)
        self.assertEqual(self.prepare(browser, clock), "disinfection_saved")
        self.assertGreaterEqual(clock.now, 6.0)
        self.assertLess(clock.now, 6.3)
        self.last_readback.assert_called_once()

    def test_no_prompt_or_unknown_retains_budget_and_requires_readback(self):
        for prompts in ([], [(1.0, "native", "權限狀態不明")]):
            with self.subTest(prompts=bool(prompts)):
                clock = VirtualClock()
                browser = ConfirmationBrowser(clock, prompts, navigation_at=1.2)
                self.assertEqual(self.prepare(browser, clock), "disinfection_saved")
                self.assertGreaterEqual(clock.now, 10.0)
                self.last_readback.assert_called_once()
                self.assertEqual(browser.save_clicks, 1)

    def test_unreliable_navigation_stamp_retains_native_tail_budget(self):
        clock = VirtualClock()
        browser = ConfirmationBrowser(clock, [(1.0, "native", "儲存成功")], navigation_at=1.4, stamp=False)
        self.assertEqual(self.prepare(browser, clock), "disinfection_saved")
        self.assertGreaterEqual(clock.now, 6.0)
        self.assertLess(clock.now, 6.3)

    def test_jitter_at_idle_deadline_still_requires_persistence_readback(self):
        for prompts in ([], [(1.0, "native", "權限狀態不明")]):
            with self.subTest(prompts=bool(prompts)):
                clock = JitterClock()
                browser = ConfirmationBrowser(clock, prompts)
                self.assertEqual(self.prepare(browser, clock), "disinfection_saved")
                self.last_readback.assert_called_once()
                self.assertEqual(browser.save_clicks, 1)
                self.assertGreaterEqual(clock.now, 10.0)
                self.assertLess(clock.now, 10.3)

    def test_early_sweetalert_retains_original_native_observation_window(self):
        clock = VirtualClock()
        browser = ConfirmationBrowser(clock, [(1.0, "sweetalert", "儲存成功"), (4.0, "native", "儲存失敗")])
        with self.assertRaisesRegex(runtime.WebDriverException, "儲存失敗"):
            self.prepare(browser, clock)
        self.last_readback.assert_not_called()

    def test_late_sweetalert_keeps_tail_beyond_initial_nine_second_budget(self):
        clock = VirtualClock()
        browser = ConfirmationBrowser(clock, [(8.8, "sweetalert", "儲存成功"), (10.5, "native", "儲存失敗")])
        with self.assertRaisesRegex(runtime.WebDriverException, "儲存失敗"):
            self.prepare(browser, clock)
        self.last_readback.assert_not_called()
        self.assertGreaterEqual(clock.now, 10.5)

    def test_unknown_after_success_restores_original_budget_for_late_failure(self):
        clock = VirtualClock()
        browser = ConfirmationBrowser(clock, [
            (1.0, "native", "儲存成功"), (3.0, "native", "狀態不明"), (9.4, "native", "儲存失敗"),
        ])
        with self.assertRaisesRegex(runtime.WebDriverException, "儲存失敗"):
            self.prepare(browser, clock)
        self.last_readback.assert_not_called()

    def test_terminal_sweetalert_storm_has_hard_observation_limit(self):
        clock = VirtualClock()
        browser = ConfirmationBrowser(clock, [(1.0 + i * 0.2, "sweetalert", "儲存成功") for i in range(100)])
        self.assertEqual(self.prepare(browser, clock), "disinfection_waiting_confirmation")
        self.assertLess(clock.now, 11.5)
        self.last_readback.assert_not_called()

    def test_cross_document_login_page_cannot_be_reported_saved(self):
        clock = VirtualClock()
        browser = ConfirmationBrowser(clock, [(1.0, "native", "儲存成功")], navigation_at=1.4, login_at=1.4)
        with self.assertRaisesRegex(runtime.WebDriverException, "login page"):
            self.prepare(browser, clock)
        self.last_readback.assert_not_called()

    def test_persistence_mismatch_remains_waiting_after_terminal_success(self):
        clock = VirtualClock()
        browser = ConfirmationBrowser(clock, [(1.0, "native", "儲存成功")], navigation_at=1.4)
        status = self.prepare(browser, clock, readback_error=runtime.WebDriverException("保存內容不符"))
        self.assertEqual(status, "disinfection_waiting_confirmation")
        self.last_readback.assert_called_once()
        self.assertEqual(browser.save_clicks, 1)

    def test_cancel_during_observation_stops_without_readback_or_resubmit(self):
        clock = VirtualClock()
        browser = ConfirmationBrowser(clock)
        with self.assertRaises(TaskCancellationError):
            self.prepare(browser, clock, cancel_at=2.0)
        self.assertLess(clock.now, 2.4)
        self.last_readback.assert_not_called()
        self.assertEqual(browser.save_clicks, 1)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for browser script contract")
    def test_real_script_waits_for_modal_to_disappear_without_reclicking(self):
        clock = VirtualClock()
        browser = ScriptContractBrowser(clock, navigation_at=1.2, modal_until=2.0)
        self.assertEqual(self.prepare(browser, clock), "disinfection_saved")
        self.assertGreaterEqual(clock.now, 2.0)
        self.assertLess(clock.now, 2.4)
        self.assertEqual(browser.modal_clicks, 1)
        self.last_readback.assert_called_once()

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for browser script contract")
    def test_real_script_reused_overlay_handles_new_text_and_button(self):
        clock = VirtualClock()
        browser = ScriptContractBrowser(
            clock, navigation_at=1.6, modal_until=2.0,
            text_changes=[(0, "請確認"), (1.4, "儲存成功")], button_changes=[(1.4, "second")],
        )
        self.assertEqual(self.prepare(browser, clock), "disinfection_saved")
        self.assertEqual(browser.modal_clicks, 2)
        self.last_readback.assert_called_once()

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for browser script contract")
    def test_real_script_hidden_then_reused_identical_overlay_is_new_prompt(self):
        clock = VirtualClock()
        browser = ScriptContractBrowser(
            clock, navigation_at=2.4, modal_until=3.0, hidden_ranges=[(1.2, 1.6)],
        )
        self.assertEqual(self.prepare(browser, clock), "disinfection_saved")
        self.assertEqual(browser.modal_clicks, 2)
        self.last_readback.assert_called_once()

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for browser script contract")
    def test_real_script_pending_or_disabled_modal_remains_waiting(self):
        for options in ({"enabled": False}, {"has_button": False}, {}):
            with self.subTest(options=options):
                clock = VirtualClock()
                browser = ScriptContractBrowser(clock, navigation_at=1.2, modal_until=20.0, **options)
                self.assertEqual(self.prepare(browser, clock), "disinfection_waiting_confirmation")
                self.assertGreaterEqual(clock.now, 10.0)
                self.assertLess(clock.now, 10.3)
                self.last_readback.assert_not_called()
                self.assertLessEqual(browser.modal_clicks, 1)
                self.assertEqual(browser.save_clicks, 1)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for browser script contract")
    def test_real_script_unhandled_unknown_modal_restores_budget_after_disappearing(self):
        clock = VirtualClock()
        browser = ScriptContractBrowser(
            clock, [(1.0, "native", "儲存成功")], enabled=False, modal_until=2.0,
            text_changes=[(0, "權限狀態不明")],
        )
        self.assertEqual(self.prepare(browser, clock), "disinfection_saved")
        self.assertGreaterEqual(clock.now, 10.0)
        self.assertEqual(browser.modal_clicks, 0)
        self.last_readback.assert_called_once()

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for browser script contract")
    def test_real_script_requires_complete_new_document(self):
        clock = VirtualClock()
        browser = ScriptContractBrowser(
            clock, [(1.0, "native", "儲存成功")], navigation_at=1.2, modal_until=0, ready_at=2.4,
        )
        self.assertEqual(self.prepare(browser, clock), "disinfection_saved")
        self.assertGreaterEqual(clock.now, 2.4)
        self.assertLess(clock.now, 2.8)

    def test_driver_error_is_not_swallowed_as_success(self):
        clock = VirtualClock()
        browser = ConfirmationBrowser(clock)
        original_execute = browser.execute_script

        def execute(script, *args):
            if "swal-overlay" in script:
                raise runtime.WebDriverException("session lost")
            return original_execute(script, *args)

        browser.execute_script = execute
        with self.assertRaisesRegex(runtime.WebDriverException, "session lost"):
            self.prepare(browser, clock)
        self.last_readback.assert_not_called()

    def test_alert_raised_between_native_probe_and_script_preserves_failure(self):
        clock = VirtualClock()
        browser = ConfirmationBrowser(clock)
        original_execute = browser.execute_script

        def execute(script, *args):
            if "swal-overlay" in script:
                raise runtime.UnexpectedAlertPresentException("late alert", alert_text="儲存失敗")
            return original_execute(script, *args)

        browser.execute_script = execute
        with self.assertRaisesRegex(runtime.WebDriverException, "儲存失敗"):
            self.prepare(browser, clock)
        self.last_readback.assert_not_called()

    def test_continuous_prompts_cannot_run_past_observation_deadline(self):
        clock = VirtualClock()
        browser = ConfirmationBrowser(clock)

        def endless_alert():
            clock.sleep(0.2)
            return SimpleNamespace(text="請確認", accept=lambda: None)

        browser.native_alert = endless_alert
        self.assertEqual(self.prepare(browser, clock), "disinfection_waiting_confirmation")
        self.assertGreaterEqual(clock.now, 10.0)
        self.assertLess(clock.now, 10.5)
        self.last_readback.assert_not_called()


if __name__ == "__main__":
    unittest.main()
