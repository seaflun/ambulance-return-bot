import json
import shutil
import subprocess
import unittest
from unittest.mock import patch

from ambulance_bot import selenium_local as runtime


class _QueryElement:
    def __init__(self, driver):
        self.driver = driver
        self.generation = driver.generation

    def is_enabled(self):
        if self.driver.pending_page is not None:
            self.driver.refresh_polls += 1
            if self.driver.refresh_polls >= 2:
                self.driver.current_page = self.driver.pending_page
                self.driver.pending_page = None
                self.driver.generation += 1
        if self.generation != self.driver.generation:
            raise runtime.StaleElementReferenceException("query document reloaded")
        return True


class _CasePageDriver:
    def __init__(self, pages, current_page=1, duplicate_page_ids=True):
        self.pages = pages
        self.current_page = current_page
        self.duplicate_page_ids = duplicate_page_ids
        self.pending_page = None
        self.refresh_polls = 0
        self.generation = 0
        self.loading = False
        self.clicked = []
        self.navigation = []
        self.clicked_while_loading = False
        self.complete_checks = 0

    def find_element(self, _by, value):
        if value != "_btnQuery":
            raise runtime.NoSuchElementException(value)
        return _QueryElement(self)

    def find_elements(self, _by, value):
        return [self.find_element(_by, value)] if value == "_btnQuery" else []

    def execute_script(self, script, *args):
        harness = """
        const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
        let requestedPage = null, clicked = [];
        const options = Object.keys(input.pages).map(value => ({value}));
        const select = {value: String(input.current), options,
          dispatchEvent: () => {requestedPage = Number(select.value);}};
        const label = {innerText: '下一頁', id: 'pageSelect'};
        const rows = input.pages[String(input.current)].map(caseId => {
          const button = {value: '選擇', innerText: '', title: '', id: 'choose', name: 'choose',
            disabled: false, getAttribute: () => '', click: () => clicked.push(caseId)};
          return {children: [{tagName: 'TD', innerText: caseId}],
            querySelectorAll: () => [button]};
        });
        const text = '共 ' + (Object.keys(input.pages).length * 10) + ' 筆';
        global.document = {readyState: input.loading ? 'loading' : 'complete', body: {innerText: text},
          getElementById: id => id === 'pageSelect' ? (input.duplicate ? label : select) :
            (id === 'selectedPage' ? {value: String(input.current)} : (id === '_btnQuery' ? {} : null)),
          querySelector: selector => selector === 'select[name="pageSelect"]' || selector === "select[name='pageSelect']"
            || selector === 'select#pageSelect' ? select : (selector === '.page' ? {innerText: text} : null),
          querySelectorAll: selector => selector === 'tr' ? rows : []};
        global.window = {getComputedStyle: () => ({display: 'block', visibility: 'visible'})};
        global.Event = class {constructor(type) {this.type = type;}};
        const value = new Function(input.script)(...input.args);
        process.stdout.write(JSON.stringify({value, requestedPage, clicked}));
        """
        result = subprocess.run([shutil.which("node"), "-e", harness],
                                input=json.dumps({"script": script, "args": args, "pages": self.pages,
                                                  "current": self.current_page, "loading": self.loading,
                                                  "duplicate": self.duplicate_page_ids}),
                                text=True, capture_output=True, encoding="utf-8", check=True)
        value = json.loads(result.stdout)
        if value["requestedPage"] is not None:
            self.navigation.append(value["requestedPage"])
            self.pending_page = value["requestedPage"]
            self.refresh_polls = 0
            self.loading = True
        if value["clicked"]:
            self.clicked.extend(value["clicked"])
            self.clicked_while_loading |= self.loading
        if "document.readyState" in script:
            self.complete_checks += 1
            if self.loading and self.pending_page is None:
                self.loading = False
        return value["value"]


@unittest.skipUnless(shutil.which("node"), "Node.js required for case-page script tests")
class DutyCasePaginationTests(unittest.TestCase):
    target = "20260801000100001"
    other = "20260801000200001"
    third = "20260801000300001"

    def choose(self, driver, case_id):
        real_wait = runtime.WebDriverWait
        with patch.object(runtime, "WebDriverWait",
                          side_effect=lambda d, t, **kw: real_wait(d, 0.4, poll_frequency=0.001)):
            return runtime._click_case_choose(driver, case_id)

    def test_case_found_on_first_page_is_selected_after_all_pages_left_last_page(self):
        driver = _CasePageDriver({1: [self.target], 2: [self.other]}, current_page=2)
        self.assertTrue(self.choose(driver, self.target))
        self.assertEqual([self.target], driver.clicked)
        self.assertEqual([1], driver.navigation)
        self.assertEqual(1, driver.current_page)

    def test_duplicate_page_ids_and_reload_do_not_allow_clicking_stale_or_loading_rows(self):
        driver = _CasePageDriver({1: [self.target], 2: [self.other]}, current_page=2,
                                 duplicate_page_ids=True)
        self.assertTrue(self.choose(driver, self.target))
        self.assertGreaterEqual(driver.refresh_polls, 2)
        self.assertGreaterEqual(driver.complete_checks, 2)
        self.assertFalse(driver.clicked_while_loading)
        self.assertEqual([self.target], driver.clicked)

    def test_searches_forward_again_and_clicks_only_the_requested_exact_case(self):
        driver = _CasePageDriver({1: [self.third], 2: [self.target], 3: [self.other]}, current_page=3)
        self.assertTrue(self.choose(driver, self.target))
        self.assertEqual([1, 2], driver.navigation)
        self.assertEqual([self.target], driver.clicked)

    def test_missing_case_never_clicks_another_case(self):
        driver = _CasePageDriver({1: [self.other], 2: [self.third]}, current_page=2)
        self.assertFalse(self.choose(driver, self.target))
        self.assertEqual([], driver.clicked)

    def test_case_already_visible_does_not_navigate(self):
        driver = _CasePageDriver({1: [self.target], 2: [self.other]})
        self.assertTrue(self.choose(driver, self.target))
        self.assertEqual([], driver.navigation)
        self.assertEqual([self.target], driver.clicked)

    def test_initial_loading_document_is_not_clicked_before_completion(self):
        driver = _CasePageDriver({1: [self.target]})
        driver.loading = True
        self.assertTrue(self.choose(driver, self.target))
        self.assertGreaterEqual(driver.complete_checks, 2)
        self.assertFalse(driver.clicked_while_loading)
        self.assertEqual([self.target], driver.clicked)
