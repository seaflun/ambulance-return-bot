import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import worker as module


class ConsumablesStallDiagnosticsTests(unittest.TestCase):
    def report(self, rows, *, status="completed"):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tasks = root / "tasks"
            tasks.mkdir()
            task = tasks / "private-task.json"
            original = json.dumps({"task": {"case_id": "PRIVATE_CASE", "name": "PRIVATE_NAME"},
                                   "site_attempts": {"consumables": rows}}).encode()
            task.write_bytes(original)
            result = root / "update-result.json"
            result.write_text(json.dumps({"request_id": "update-test", "status": status,
                                          "detail": "更新完成", "exit_code": 0}), encoding="utf-8")
            posts = []
            with patch.dict(module.os.environ, {"ARTIFACTS_DIR": str(root)}), patch.object(
                module, "remote_update_result_path", return_value=result
            ):
                self.assertTrue(module.report_remote_update_result(
                    "http://nas", "public-duty-pc", post_command_status=lambda *args, **kwargs: posts.append(args)
                ))
            self.assertEqual(task.read_bytes(), original)
            return posts[0][3]

    def slow_rows(self):
        return [{"time": "2026-10-19T19:09:30", "status": "consumables_running", "detail": "本機快速執行中。"},
                {"time": "2026-10-19T19:09:31", "status": "consumables_running", "detail": "本機快速 一站通耗材：啟動 Chrome"},
                {"time": "2026-10-19T19:09:35", "status": "consumables_running", "detail": "本機快速 一站通耗材：登入一站通"},
                {"time": "2026-10-19T19:19:35", "status": "consumables_running", "detail": "本機快速 一站通耗材：開啟耗材紀錄；PRIVATE_CASE PRIVATE_NAME"},
                {"time": "2026-10-19T19:20:02", "status": "consumables_saved", "detail": "PRIVATE_CASE PRIVATE_NAME saved"}]

    def test_update_result_returns_only_existing_slow_run_stage_times(self):
        detail = self.report(self.slow_rows())
        self.assertIn("耗材慢速階段=", detail)
        diagnostic = json.loads(detail.split("耗材慢速階段=", 1)[1])
        self.assertEqual(diagnostic["seconds"], 632)
        self.assertEqual(diagnostic["stages"][-1], ["開啟耗材紀錄", "2026-10-19T19:19:35+08:00"])
        self.assertNotIn("PRIVATE", detail)
        self.assertNotIn("case_id", detail)

    def test_short_run_does_not_add_diagnostics(self):
        rows = self.slow_rows()[:2] + [{"time": "2026-10-19T19:10:02", "status": "consumables_saved"}]
        self.assertEqual(self.report(rows), "更新完成")

    def test_failed_update_does_not_append_unrelated_completed_task(self):
        self.assertEqual(self.report(self.slow_rows(), status="failed"), "更新完成")
