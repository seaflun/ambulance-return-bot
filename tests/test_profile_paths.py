import contextlib
import io
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import ambulance_bot.profile_paths as profile_paths


class ProfilePathTests(unittest.TestCase):
    def test_runtime_profile_dir_continues_when_stale_cleanup_hits_invalid_argument(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "profiles"
            previous_root = os.environ.get("SELENIUM_PROFILE_ROOT")
            output = io.StringIO()
            try:
                os.environ["SELENIUM_PROFILE_ROOT"] = str(root)
                with mock.patch.object(
                    profile_paths,
                    "cleanup_stale_runtime_profiles",
                    side_effect=OSError(22, "Invalid argument"),
                ):
                    with contextlib.redirect_stdout(output):
                        profile = profile_paths.runtime_profile_dir("duty_work_log_profile_task1")
            finally:
                if previous_root is None:
                    os.environ.pop("SELENIUM_PROFILE_ROOT", None)
                else:
                    os.environ["SELENIUM_PROFILE_ROOT"] = previous_root

            self.assertEqual(profile, root / "duty_work_log_profile_task1")
            self.assertTrue(profile.is_dir())
            self.assertIn("profile cleanup unavailable", output.getvalue())
            self.assertIn("Invalid argument", output.getvalue())

    def test_cleanup_stale_runtime_profiles_skips_invalid_profile_root_iteration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = io.StringIO()
            with mock.patch.object(Path, "iterdir", side_effect=OSError(22, "Invalid argument")):
                with contextlib.redirect_stdout(output):
                    removed = profile_paths.cleanup_stale_runtime_profiles(root)

            self.assertEqual(removed, [])
            self.assertIn("profile cleanup unavailable", output.getvalue())
            self.assertIn("Invalid argument", output.getvalue())

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

    def test_cleanup_stale_runtime_profiles_silently_skips_windows_locked_profiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            locked_profile = root / "case_lookup_profile_123.chrome_repair_20260705_123142"
            locked_profile.mkdir()
            (locked_profile / "Default").mkdir()
            (locked_profile / "Default" / "Login Data-journal").write_text("locked", encoding="utf-8")
            old_time = time.time() - 7200
            os.utime(locked_profile / "Default" / "Login Data-journal", (old_time, old_time))
            os.utime(locked_profile / "Default", (old_time, old_time))
            os.utime(locked_profile, (old_time, old_time))
            error = PermissionError(5, "Access is denied", str(locked_profile / "Default" / "Login Data-journal"))

            output = io.StringIO()
            with mock.patch.object(profile_paths.shutil, "rmtree", side_effect=error):
                with contextlib.redirect_stdout(output):
                    removed = profile_paths.cleanup_stale_runtime_profiles(root, max_age_hours=1)

            self.assertEqual(removed, [])
            self.assertTrue(locked_profile.exists())
            self.assertEqual(output.getvalue(), "")

    def test_cleanup_stale_runtime_profiles_removes_old_repair_backups(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_backup = root / "case_lookup_profile_123.chrome_repair_20260705_123142"
            worker_backup = root / "worker_browser_profile.chrome_repair_20260705_123142"
            old_backup.mkdir()
            worker_backup.mkdir()
            (old_backup / "cache.dat").write_text("old", encoding="utf-8")
            (worker_backup / "cache.dat").write_text("old", encoding="utf-8")
            unknown_backup = root / "personal_profile.chrome_repair_20260705_123142"
            unknown_backup.mkdir()
            old_time = time.time() - 7200
            os.utime(old_backup / "cache.dat", (old_time, old_time))
            os.utime(old_backup, (old_time, old_time))
            os.utime(worker_backup / "cache.dat", (old_time, old_time))
            os.utime(worker_backup, (old_time, old_time))
            os.utime(unknown_backup, (old_time, old_time))

            removed = profile_paths.cleanup_stale_runtime_profiles(root, max_age_hours=1)

            self.assertEqual(
                {path.name for path in removed},
                {
                    "case_lookup_profile_123.chrome_repair_20260705_123142",
                    "worker_browser_profile.chrome_repair_20260705_123142",
                },
            )
            self.assertFalse(old_backup.exists())
            self.assertFalse(worker_backup.exists())
            self.assertTrue(unknown_backup.exists())

    def test_cleanup_runtime_profiles_for_startup_failure_removes_generated_profiles_and_backups(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current_profile = root / "case_lookup_profile_123"
            repair_backup = root / "worker_browser_profile.chrome_repair_20260705_123142"
            normal_chrome_profile = root / "Chrome User Data"
            for path in (current_profile, repair_backup, normal_chrome_profile):
                path.mkdir()
                (path / "cache.dat").write_text("old", encoding="utf-8")

            removed = profile_paths.cleanup_runtime_profiles_for_startup_failure([current_profile], profile_root=root)

            self.assertEqual({path.name for path in removed}, {"case_lookup_profile_123", "worker_browser_profile.chrome_repair_20260705_123142"})
            self.assertFalse(current_profile.exists())
            self.assertFalse(repair_backup.exists())
            self.assertTrue(normal_chrome_profile.exists())


if __name__ == "__main__":
    unittest.main()
