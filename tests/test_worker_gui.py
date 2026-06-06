import os
import tempfile
import unittest
from pathlib import Path

import worker_gui


class WorkerGuiEnvTests(unittest.TestCase):
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
