import json
import tempfile
import unittest
from pathlib import Path

from ambulance_bot.civilpower_preferences import (
    load_frequent_member_ids,
    normalize_frequent_member_ids,
    save_frequent_member_ids,
)


class CivilpowerPreferencesTests(unittest.TestCase):
    def test_normalize_keeps_first_ordered_nonempty_member_ids(self):
        member_ids = normalize_frequent_member_ids(
            [" 江尚諭 ", "", "張贊鏡", "江尚諭", None, 123, "  張贊鏡  "]
        )

        self.assertEqual(["江尚諭", "張贊鏡"], member_ids)

    def test_missing_or_malformed_preferences_load_as_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifacts_dir = Path(tmp)
            self.assertEqual([], load_frequent_member_ids(artifacts_dir))

            path = artifacts_dir / "settings" / "civilpower_volunteer_preferences.json"
            path.parent.mkdir(parents=True)
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual([], load_frequent_member_ids(artifacts_dir))

            path.write_text('{"member_ids": ["江尚諭"]}', encoding="utf-8")
            self.assertEqual([], load_frequent_member_ids(artifacts_dir))

    def test_save_normalizes_and_round_trips_from_artifacts_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifacts_dir = Path(tmp)

            saved = save_frequent_member_ids(
                [" 江尚諭 ", "張贊鏡", "江尚諭", ""],
                artifacts_dir,
            )

            path = artifacts_dir / "settings" / "civilpower_volunteer_preferences.json"
            self.assertEqual(["江尚諭", "張贊鏡"], saved)
            self.assertEqual(["江尚諭", "張贊鏡"], json.loads(path.read_text(encoding="utf-8")))
            self.assertEqual(["江尚諭", "張贊鏡"], load_frequent_member_ids(artifacts_dir))


if __name__ == "__main__":
    unittest.main()
