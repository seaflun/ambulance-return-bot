import unittest
from unittest.mock import patch

from selenium.common.exceptions import NoSuchElementException, NoSuchFrameException, StaleElementReferenceException
from selenium.webdriver.remote.command import Command
from selenium.webdriver.remote.switch_to import SwitchTo

from ambulance_bot import selenium_local as runtime
from ambulance_bot.task_cancellation import TaskCancellationError


class Clock:
    def __init__(self):
        self.now = 0.0

    def sleep(self, seconds):
        self.now += seconds


class ContextBrowser:
    def __init__(self, clock, states):
        self.clock = clock
        self.states = list(states)
        self.switch_to = SwitchTo(self)
        self.frame = None
        self.finds = []
        self.probes = 0
        self.switch_errors = []

    def find_element(self, by, value):
        self.finds.append((by, value))
        self.clock.now += 2
        raise NoSuchElementException(value)

    def execute(self, command, params):
        if command == Command.SWITCH_TO_FRAME:
            if params['id'] is not None and self.switch_errors:
                raise self.switch_errors.pop(0)
            self.frame = params['id']
        return {}

    def execute_script(self, script):
        self.probes += 1
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        if isinstance(state, Exception):
            raise state
        return state


class DisinfectionContextLatencyTests(unittest.TestCase):
    def select_context(self, browser, clock, **kwargs):
        with patch.object(runtime.time, 'monotonic', side_effect=lambda: clock.now), \
             patch.object(runtime.time, 'sleep', side_effect=clock.sleep):
            runtime._switch_to_disinfection_content_if_present(browser, **kwargs)

    def test_ready_top_level_query_skips_implicit_frame_search(self):
        clock = Clock()
        browser = ContextBrowser(clock, [dict(frame=None, query_ready=True)])
        self.select_context(browser, clock)
        self.assertEqual(clock.now, 0)
        self.assertEqual(browser.finds, [])
        self.assertIsNone(browser.frame)

    def test_existing_frame_is_selected_before_outer_query_fields(self):
        clock = Clock()
        frame = object()
        browser = ContextBrowser(clock, [dict(frame=frame, query_ready=True)])
        self.select_context(browser, clock)
        self.assertIs(browser.frame, frame)
        self.assertEqual(browser.finds, [])

    def test_late_frame_is_selected_after_short_condition_wait(self):
        clock = Clock()
        frame = object()
        browser = ContextBrowser(clock, [dict(frame=None, query_ready=False), dict(frame=frame, query_ready=False)])
        self.select_context(browser, clock)
        self.assertIs(browser.frame, frame)
        self.assertGreater(clock.now, 0)
        self.assertLess(clock.now, 1)

    def test_late_top_level_fields_are_accepted_without_frame_search(self):
        clock = Clock()
        browser = ContextBrowser(clock, [dict(frame=None, query_ready=False), dict(frame=None, query_ready=True)])
        self.select_context(browser, clock)
        self.assertEqual(browser.finds, [])
        self.assertGreater(clock.now, 0)
        self.assertLess(clock.now, 1)

    def test_replaced_frame_is_probed_again_after_transient_switch_failure(self):
        for error in (NoSuchFrameException('reloaded'), StaleElementReferenceException('replaced')):
            with self.subTest(error=type(error).__name__):
                clock = Clock()
                old_frame, new_frame = object(), object()
                browser = ContextBrowser(clock, [dict(frame=old_frame, query_ready=False),
                                                 dict(frame=new_frame, query_ready=False)])
                browser.switch_errors.append(error)
                self.select_context(browser, clock)
                self.assertIs(browser.frame, new_frame)
                self.assertEqual(browser.probes, 2)

    def test_unready_or_login_document_has_bounded_failure(self):
        clock = Clock()
        browser = ContextBrowser(clock, [dict(frame=None, query_ready=False)])
        with self.assertRaises(runtime.TimeoutException):
            self.select_context(browser, clock, timeout=0.5)
        self.assertAlmostEqual(clock.now, 0.5)
        self.assertIsNone(browser.frame)
        self.assertEqual(browser.finds, [])

    def test_frame_churn_does_not_reset_total_wait_budget(self):
        clock = Clock()
        browser = ContextBrowser(clock, [dict(frame=object(), query_ready=False)])
        browser.switch_errors = [NoSuchFrameException('reloaded') for _ in range(10)]
        with self.assertRaises(runtime.TimeoutException):
            self.select_context(browser, clock, timeout=0.5)
        self.assertAlmostEqual(clock.now, 0.5)

    def test_cancellation_during_context_wait_propagates(self):
        clock = Clock()
        browser = ContextBrowser(clock, [dict(frame=None, query_ready=False)])

        def cancel():
            if clock.now > 0:
                raise TaskCancellationError('cancelled')

        with self.assertRaises(TaskCancellationError):
            self.select_context(browser, clock, cancel_check=cancel)
        self.assertLess(clock.now, 1)

    def test_session_failure_is_not_hidden_as_optional_frame_absence(self):
        clock = Clock()
        browser = ContextBrowser(clock, [runtime.WebDriverException('invalid session id')])
        with self.assertRaisesRegex(runtime.WebDriverException, 'invalid session id'):
            self.select_context(browser, clock)
        self.assertEqual(clock.now, 0)


if __name__ == '__main__':
    unittest.main()
