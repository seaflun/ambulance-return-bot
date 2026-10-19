import copy
import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest
from unittest import mock

import app as app_module
from ambulance_bot import sinposmart_backend as backend


class DisasterActorNumberOrderTests(unittest.TestCase):
    def test_browser_rebuild_uses_numeric_actor_order_and_ignores_inherited_numbers(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js required for personnel sorting behavior tests")
        source = (Path(__file__).resolve().parents[1] / "WinPython_公務電腦使用包/templates/disaster_task.html").read_text(encoding="utf-8")
        functions = re.search(r"(?ms)^  function personnelDisplayRank\(.*?(?=^  function escapeHtml\()", source).group(0)
        script = r"""
const source = require('fs').readFileSync(0, 'utf8');
const personnelTitles = {員十: '隊員', 員二: '隊員', 小四: '小隊長', 小三: '小隊長', 分長: '分隊長'};
const personnelActorNumbers = Object.assign(Object.create({constructor: 1}), {員十: 10, 員二: 2, 小四: 4, 小三: 3, 分長: 1});
let sorted;
const context = {personnelTitles, personnelActorNumbers,
  form: {elements: {personnel: {value: '員十,constructor,小四,分長,員二,小三,未知'}}},
  currentFormValues: () => [], currentVehiclePackages: {}, currentVehicleButtons: {},
  currentPersonnelPackages: {}, currentPersonnelButtons: {},
  updateCurrentPackageButtons: (section, buttons, values, field) => {if (field === 'currentPersonnelPackage') sorted = values;}};
require('vm').runInNewContext(source, context);
context.updateCurrentPackages();
process.stdout.write(JSON.stringify(sorted));
"""
        result = subprocess.run([node, "-e", script], input=functions, text=True, encoding="utf-8", capture_output=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(["分長", "小三", "小四", "員二", "員十", "constructor", "未知"], json.loads(result.stdout))

    def snapshot(self):
        return {"schema_version": 1, "source": "official_duty_staff", "target_date": "2026-10-19",
                "complete": True, "members": [
                    {"actor_no": "10", "name": "員十", "title": "隊員"},
                    {"actor_no": "2", "name": "員二", "title": "隊員"},
                    {"actor_no": "1", "name": "分長", "title": "分隊長"},
                    {"actor_no": "4", "name": "小四", "title": "小隊長"},
                    {"actor_no": "3", "name": "小三", "title": "小隊長"}]}

    def test_three_orders_use_numeric_actor_numbers_within_each_rank(self):
        case = {"case_date": "2026/10/19", "personnel": ["員十", "未知乙", "小四", "分長", "員二", "小三", "未知甲"]}
        original = copy.deepcopy(case)
        with app_module.app.test_request_context(base_url="http://nas.test:8080"), mock.patch.object(
            app_module, "read_personnel_title_snapshot", return_value=self.snapshot()
        ):
            result = app_module.disaster_personnel_display_options(case)
        self.assertEqual(["分長", "小三", "小四", "員二", "員十", "未知乙", "未知甲"],
                         [name for name, _ in result["current_personnel_options"]])
        self.assertEqual(result["current_personnel_options"], result["firecam_person_options"])
        self.assertEqual(["員二", "員十", "小三", "小四", "分長", "未知乙", "未知甲"],
                         [name for name, _ in result["driver_person_options"]])
        self.assertEqual({"員十": 10, "員二": 2, "分長": 1, "小四": 4, "小三": 3}, result["personnel_actor_numbers"])
        self.assertEqual(original, case)

    def test_actor_numbers_share_exact_date_complete_and_ambiguity_guards(self):
        select = getattr(backend, "personnel_actor_numbers_for_case", None)
        self.assertIsNotNone(select)
        snapshot = self.snapshot()
        self.assertEqual({"員十": 10, "員二": 2}, select(snapshot, "2026/10/19", ["員十", "員二"]))
        self.assertEqual({}, select(snapshot, "2026/10/18", ["員十"]))
        self.assertEqual({}, select({**snapshot, "complete": False}, "2026/10/19", ["員十"]))
        snapshot["members"].append({"actor_no": "02", "name": "別人", "title": "隊員"})
        self.assertEqual({}, select(snapshot, "2026/10/19", ["員二", "別人"]))
        snapshot["members"].append({"actor_no": "11", "name": "員十", "title": "隊員"})
        self.assertEqual({}, select(snapshot, "2026/10/19", ["員十"]))


if __name__ == "__main__":
    unittest.main()
