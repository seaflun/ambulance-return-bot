from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ReleaseScriptTests(unittest.TestCase):
    def test_release_tag_targets_the_current_commit(self) -> None:
        script = (
            PROJECT_ROOT / "scripts" / "publish_ambulance_return_release.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn("[string]$TargetCommitish", script)
        self.assertIn("git -C $project rev-parse HEAD", script)
        self.assertIn("--target $TargetCommitish", script)


if __name__ == "__main__":
    unittest.main()
