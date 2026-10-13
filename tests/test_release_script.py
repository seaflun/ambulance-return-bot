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

    def test_notes_check_precedes_publication_in_both_release_paths(self) -> None:
        script = (PROJECT_ROOT / "scripts" / "publish_ambulance_return_release.ps1").read_text(encoding="utf-8")
        workflow = (PROJECT_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        self.assertLess(script.index("verify_system_updates.py"), script.index("gh release create"))
        self.assertIn("--package $zipPath", script)
        self.assertIn('if ($LASTEXITCODE -ne 0)', script)
        self.assertLess(workflow.index("verify_system_updates.py"), workflow.index("gh release upload"))
        self.assertIn("--package UPDATE/ambulance-return-public-package.zip", workflow)


if __name__ == "__main__":
    unittest.main()
