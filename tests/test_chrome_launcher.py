import os
import tempfile
import unittest
from pathlib import Path

import ambulance_bot.chrome_launcher as chrome_launcher


class ChromeLauncherTests(unittest.TestCase):
    def test_open_url_uses_worker_browser_profile_under_runtime_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            previous_profile = os.environ.get("CHROME_PROFILE_DIR")
            previous_root = os.environ.get("SELENIUM_PROFILE_ROOT")
            previous_port = os.environ.get("WORKER_CHROME_DEBUGGER_PORT")
            original_binary = chrome_launcher._chrome_binary
            original_popen = chrome_launcher.subprocess.Popen
            calls = []
            try:
                os.environ.pop("CHROME_PROFILE_DIR", None)
                os.environ["SELENIUM_PROFILE_ROOT"] = str(Path(tmp) / "profiles")
                os.environ["WORKER_CHROME_DEBUGGER_PORT"] = "9223"
                chrome_launcher._chrome_binary = lambda: Path(r"C:\Chrome\chrome.exe")
                chrome_launcher.subprocess.Popen = lambda args, **kwargs: calls.append(args)

                status = chrome_launcher.open_url_in_worker_chrome("about:blank")
            finally:
                chrome_launcher._chrome_binary = original_binary
                chrome_launcher.subprocess.Popen = original_popen
                if previous_profile is None:
                    os.environ.pop("CHROME_PROFILE_DIR", None)
                else:
                    os.environ["CHROME_PROFILE_DIR"] = previous_profile
                if previous_root is None:
                    os.environ.pop("SELENIUM_PROFILE_ROOT", None)
                else:
                    os.environ["SELENIUM_PROFILE_ROOT"] = previous_root
                if previous_port is None:
                    os.environ.pop("WORKER_CHROME_DEBUGGER_PORT", None)
                else:
                    os.environ["WORKER_CHROME_DEBUGGER_PORT"] = previous_port

            self.assertEqual(status, "opened_worker_chrome")
            self.assertEqual(len(calls), 1)
            self.assertIn(f"--user-data-dir={Path(tmp) / 'profiles' / 'worker_browser_profile'}", calls[0])
            self.assertIn("--remote-debugging-port=9223", calls[0])


if __name__ == "__main__":
    unittest.main()
