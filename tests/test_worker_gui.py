import os
import tempfile
import unittest
from pathlib import Path

import worker_gui


class WorkerGuiEnvTests(unittest.TestCase):
    def test_task_row_values_formats_payload(self):
        task_id, values = worker_gui.task_row_values(
            {
                "overall_status": "queued_for_worker",
                "task": {
                    "task_id": "task-1",
                    "vehicle": "新坡91",
                    "driver": "曾彥綸",
                    "case_time": "1420",
                    "return_time": "1505",
                    "case_address": "桃園市觀音區",
                },
            }
        )

        self.assertEqual(task_id, "task-1")
        self.assertEqual(values, ("queued_for_worker", "新坡91", "曾彥綸", "1420/1505", "桃園市觀音區"))

    def test_update_env_values_replaces_and_appends(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("A=1\nDUTY_ACCOUNT=old\n", encoding="utf-8")
            old_path = os.environ.get("DOTENV_PATH")
            os.environ["DOTENV_PATH"] = str(path)
            try:
                worker_gui.update_env_values(
                    {
                        "DUTY_ACCOUNT": "new-account",
                        "DUTY_PASSWORD": "new-password",
                    }
                )
            finally:
                if old_path is None:
                    os.environ.pop("DOTENV_PATH", None)
                else:
                    os.environ["DOTENV_PATH"] = old_path

            text = path.read_text(encoding="utf-8")
            self.assertIn("A=1", text)
            self.assertIn("DUTY_ACCOUNT=new-account", text)
            self.assertIn("DUTY_PASSWORD=new-password", text)
            self.assertNotIn("DUTY_ACCOUNT=old", text)


if __name__ == "__main__":
    unittest.main()
