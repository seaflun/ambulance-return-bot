import copy
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest

import app as app_module


TEMPLATE = Path(__file__).resolve().parents[1] / "WinPython_公務電腦使用包/templates/disaster_task.html"
PEOPLE = ["隊乙", "未知乙", "小甲", "分甲", "隊甲", "小乙", "未知甲", "constructor"]
TITLES = {"隊乙": "隊員", "小甲": "小隊長", "分甲": "分隊長", "隊甲": "隊員", "小乙": "小隊長"}
LEADERS_FIRST = ["分甲", "小甲", "小乙", "隊乙", "隊甲", "未知乙", "未知甲", "constructor"]
DRIVERS_FIRST = ["隊乙", "隊甲", "小甲", "小乙", "分甲", "未知乙", "未知甲", "constructor"]


class PersonnelFormParser(HTMLParser):
    def __init__(self, body):
        super().__init__(convert_charrefs=True)
        self.selects = []
        self.current = []
        self.firecam = []
        self.hidden = {}
        self.active_select = None
        self.active_option = None
        self.active_person = None
        self.feed(body)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "select":
            self.active_select = {"name": attrs.get("name"), "options": []}
            self.selects.append(self.active_select)
        elif tag == "option" and self.active_select is not None:
            self.active_option = {"value": attrs.get("value", ""), "label": "", "selected": "selected" in attrs}
            self.active_select["options"].append(self.active_option)
        elif tag == "button" and "data-current-personnel-package" in attrs:
            self.active_person = {"name": attrs["data-current-personnel-package"],
                                  "action": attrs["data-action-text"], "label": ""}
            self.current.append(self.active_person)
        elif tag == "input" and attrs.get("name") == "firecam_person":
            self.firecam.append({"name": attrs["value"], "checked": "checked" in attrs})
        elif tag == "input" and attrs.get("type") == "hidden":
            self.hidden[attrs.get("name")] = attrs.get("value", "")

    def handle_data(self, data):
        if self.active_option is not None:
            self.active_option["label"] += data
        if self.active_person is not None:
            self.active_person["label"] += data

    def handle_endtag(self, tag):
        if tag == "select":
            self.active_select = None
        elif tag == "option":
            self.active_option = None
        elif tag == "button":
            self.active_person = None


class DisasterPersonnelOrderTests(unittest.TestCase):
    def render_form(self, *, use_display_options=True, personnel=None):
        people = list(PEOPLE) if personnel is None else personnel
        selected_case = {"case_id": "synthetic-personnel-order", "case_date": "2026/10/19",
                         "case_time": "1200", "return_time": "1300", "personnel": people,
                         "personnel_accounts": ["a", "b"], "summary_type": "火災", "commander": "小甲",
                         "firecam_people": ["隊甲", "小乙"], "vehicle_entries": [
                             {"vehicle": "新坡11", "driver": "隊甲"},
                             {"vehicle": "新坡15", "driver": "分甲"}]}
        before = copy.deepcopy(selected_case)
        context = {"selected_case": selected_case, "form_action": "/tasks/disaster", "recent_tasks": [],
                   "case_lookup": {"is_running": False, "cases": []}, "form_errors": [], "page_notice": "",
                   "person_options": [(name, name) for name in PEOPLE], "personnel_titles": TITLES,
                   "vehicle_options": ["新坡11", "新坡15"], "vehicle_plate_names": {},
                   "disaster_reason_options_by_type": {"火災": []}, "disaster_action_packages": [],
                   "last_vehicle_mileages": {}, "last_vehicle_mileage_times": lambda: {}}
        if use_display_options:
            context.update(current_personnel_options=[(name, name) for name in LEADERS_FIRST],
                           driver_person_options=[(name, name) for name in DRIVERS_FIRST],
                           firecam_person_options=[(name, name) for name in LEADERS_FIRST])
        with app_module.app.test_request_context("/app/disaster", base_url="http://nas.test:8080"):
            body = app_module.render_template("disaster_task.html", **context)
        self.assertEqual(before, selected_case, "Rendering must not reorder the submitted case data")
        return body, PersonnelFormParser(body)

    def test_current_personnel_uses_leadership_order_and_title_name_labels(self):
        _, form = self.render_form()
        expected = [{"name": name, "label": f"{TITLES[name]} {name}" if name in TITLES else name,
                     "action": f"{TITLES[name]} {name}" if name in TITLES else name} for name in LEADERS_FIRST]
        self.assertEqual(expected, form.current)
        self.assertEqual(",".join(PEOPLE), form.hidden["personnel"])
        self.assertEqual("a,b", form.hidden["personnel_accounts"])

    def test_each_vehicle_driver_uses_member_first_order_without_changing_selected_names(self):
        _, form = self.render_form()
        drivers = [select for select in form.selects if select["name"] == "driver"]
        self.assertEqual(2, len(drivers))
        for select, chosen in zip(drivers, ("隊甲", "分甲")):
            with self.subTest(vehicle_driver=chosen):
                options = select["options"][1:]
                self.assertEqual(DRIVERS_FIRST, [option["value"] for option in options])
                self.assertEqual(DRIVERS_FIRST, [option["label"] for option in options])
                self.assertEqual([chosen], [option["value"] for option in options if option["selected"]])
        commander = next(select for select in form.selects if select["name"] == "commander")
        self.assertEqual(PEOPLE, [option["value"] for option in commander["options"][1:]])
        self.assertEqual(["小甲"], [option["value"] for option in commander["options"] if option["selected"]])

    def test_firecam_uses_leadership_order_with_names_and_preserves_checked_people(self):
        body, form = self.render_form()
        self.assertEqual(LEADERS_FIRST, [person["name"] for person in form.firecam])
        self.assertEqual({"隊甲", "小乙"}, {person["name"] for person in form.firecam if person["checked"]})
        for name in PEOPLE:
            self.assertIn(f"<span>{name}</span>", body)

    def test_legacy_direct_template_context_keeps_original_options_and_raw_personnel(self):
        _, form = self.render_form(use_display_options=False, personnel=",".join(PEOPLE))
        self.assertEqual(PEOPLE, [person["name"] for person in form.current])
        self.assertEqual(PEOPLE, [person["name"] for person in form.firecam])
        self.assertEqual(",".join(PEOPLE), form.hidden["personnel"])
        for select in form.selects:
            if select["name"] in ("commander", "driver"):
                self.assertEqual(PEOPLE, [option["value"] for option in select["options"][1:]])

    def test_dynamic_rebuild_sorts_stably_and_click_keeps_title_and_raw_form_values(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js required for disaster personnel display behavior tests")
        source = TEMPLATE.read_text(encoding="utf-8")
        append = re.search(r"(?ms)^  function appendActionText\(text\)\{.*?^  \}", source)
        buttons = re.search(r"(?ms)^  function updateCurrentPackageButtons\(.*?(?=^  function escapeHtml\()", source)
        self.assertIsNotNone(append)
        self.assertIsNotNone(buttons)
        script = r"""
const input = JSON.parse(require('fs').readFileSync(0, 'utf8')), noop = () => {};
function container() {return {children: [], replaceChildren() {this.children = [];}, appendChild(button) {this.children.push(button);}};}
const currentVehiclePackages = {}, currentPersonnelPackages = {};
const currentVehicleButtons = container(), currentPersonnelButtons = container();
const personnel = {value: input.people.join(',')}, note = {value: ''};
const driver = {value: '隊甲'}, firecam = {value: '小乙', checked: true};
const form = {elements: {personnel, driver}, querySelectorAll: () => [{value: '新坡11'}, {value: '新坡15'}]};
const document = {getElementById: () => note, createElement: () => ({dataset: {}, handlers: {},
  addEventListener(type, fn) {this.handlers[type] = fn;}})};
const context = {form, document, currentVehiclePackages, currentPersonnelPackages, currentVehicleButtons,
  currentPersonnelButtons, personnelTitles: Object.assign(Object.create({constructor: '分隊長', 未知乙: '小隊長'}), input.titles),
  updatePreview: noop, updateRequiredHints: noop};
require('vm').runInNewContext(input.source, context);
context.updateCurrentPackages();
const describe = () => currentPersonnelButtons.children.map(button => ({name: button.dataset.currentPersonnelPackage,
  label: button.textContent, action: button.dataset.actionText}));
const initial = describe();
currentPersonnelButtons.children[0].handlers.click();
personnel.value = '未知甲,隊甲,小乙,constructor,小甲,隊乙,未知乙';
context.updateCurrentPackages();
const dynamic = describe();
currentPersonnelButtons.children[0].handlers.click();
const preserved = {personnel: personnel.value, driver: driver.value, firecam};
personnel.value = '';
context.updateCurrentPackages();
process.stdout.write(JSON.stringify({initial, dynamic, note: note.value, preserved,
  empty: currentPersonnelButtons.children.length, hidden: currentPersonnelPackages.hidden,
  vehicles: currentVehicleButtons.children.map(button => button.textContent)}));
"""
        result = subprocess.run([node, "-e", script], input=json.dumps({"source": append.group(0) + "\n" + buttons.group(0),
                                                                      "people": PEOPLE, "titles": TITLES}),
                                text=True, encoding="utf-8", capture_output=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)
        state = json.loads(result.stdout)
        self.assertEqual(LEADERS_FIRST, [person["name"] for person in state["initial"]])
        for person in state["initial"] + state["dynamic"]:
            expected = f"{TITLES[person['name']]} {person['name']}" if person["name"] in TITLES else person["name"]
            self.assertEqual(expected, person["label"])
            self.assertEqual(expected, person["action"])
        self.assertEqual(["小乙", "小甲", "隊甲", "隊乙", "未知甲", "constructor", "未知乙"],
                         [person["name"] for person in state["dynamic"]])
        self.assertEqual("分隊長 分甲，小隊長 小乙", state["note"])
        self.assertEqual({"personnel": "未知甲,隊甲,小乙,constructor,小甲,隊乙,未知乙", "driver": "隊甲",
                          "firecam": {"value": "小乙", "checked": True}}, state["preserved"])
        self.assertEqual(["新坡11", "新坡15"], state["vehicles"])
        self.assertEqual(0, state["empty"])
        self.assertTrue(state["hidden"])


if __name__ == "__main__":
    unittest.main()
