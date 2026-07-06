import os
import tempfile
import time
import unittest
from pathlib import Path

import ambulance_bot.profile_paths as profile_paths


class ProfilePathTests(unittest.TestCase):
    def test_runtime_profile_dir_cleans_stale_generated_residue(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "profiles"
            root.mkdir()
            old_profile = root / "chrome_profile"
            old_profile.mkdir()
            (old_profile / "cache.dat").write_text("old", encoding="utf-8")
            recent_profile = root / "vehicle_mileage_profile_recent"
            recent_profile.mkdir()
            locked_profile = root / "disinfection_profile_old"
            locked_profile.mkdir()
            (locked_profile / "SingletonLock").write_text("", encoding="utf-8")
            unknown = root / "personal_notes"
            unknown.mkdir()
            old_time = time.time() - 7200
            for path in [old_profile, locked_profile, unknown]:
                os.utime(path, (old_time, old_time))

            previous_root = os.environ.get("SELENIUM_PROFILE_ROOT")
            previous_age = os.environ.get("SELENIUM_PROFILE_CLEANUP_MAX_AGE_HOURS")
            try:
                os.environ["SELENIUM_PROFILE_ROOT"] = str(root)
                os.environ["SELENIUM_PROFILE_CLEANUP_MAX_AGE_HOURS"] = "1"

                new_profile = profile_paths.runtime_profile_dir("consumables_profile_task1")
            finally:
                if previous_root is None:
                    os.environ.pop("SELENIUM_PROFILE_ROOT", None)
                else:
                    os.environ["SELENIUM_PROFILE_ROOT"] = previous_root
                if previous_age is None:
                    os.environ.pop("SELENIUM_PROFILE_CLEANUP_MAX_AGE_HOURS", None)
                else:
                    os.environ["SELENIUM_PROFILE_CLEANUP_MAX_AGE_HOURS"] = previous_age

            self.assertEqual(new_profile, root / "consumables_profile_task1")
            self.assertFalse(old_profile.exists())
            self.assertTrue(recent_profile.exists())
            self.assertTrue(locked_profile.exists())
            self.assertTrue(unknown.exists())

    def test_worker_browser_profile_reuses_clean_directory_after_stale_residue_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "profiles"
            old_worker_profile = root / "worker_browser_profile"
            old_worker_profile.mkdir(parents=True)
            (old_worker_profile / "cache.dat").write_text("old", encoding="utf-8")
            old_time = time.time() - 7200
            os.utime(old_worker_profile / "cache.dat", (old_time, old_time))
            os.utime(old_worker_profile, (old_time, old_time))
            previous_root = os.environ.get("SELENIUM_PROFILE_ROOT")
            previous_age = os.environ.get("SELENIUM_PROFILE_CLEANUP_MAX_AGE_HOURS")
            try:
                os.environ["SELENIUM_PROFILE_ROOT"] = str(root)
                os.environ["SELENIUM_PROFILE_CLEANUP_MAX_AGE_HOURS"] = "1"

                profile = profile_paths.worker_browser_profile_dir()
            finally:
                if previous_root is None:
                    os.environ.pop("SELENIUM_PROFILE_ROOT", None)
                else:
                    os.environ["SELENIUM_PROFILE_ROOT"] = previous_root
                if previous_age is None:
                    os.environ.pop("SELENIUM_PROFILE_CLEANUP_MAX_AGE_HOURS", None)
                else:
                    os.environ["SELENIUM_PROFILE_CLEANUP_MAX_AGE_HOURS"] = previous_age

            self.assertEqual(profile, old_worker_profile)
            self.assertTrue(profile.exists())
            self.assertFalse((profile / "cache.dat").exists())


if __name__ == "__main__":
    unittest.main()
