from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class NasComposeTests(unittest.TestCase):
    def test_disaster_recorder_mount_includes_nas_shared_folder(self) -> None:
        compose_text = (PROJECT_ROOT / "compose.nas.yml").read_text(encoding="utf-8")

        self.assertIn(
            "/volume1/nas/搶救災害硬碟/救災行車紀錄器:/data/disaster-records",
            compose_text,
        )
        self.assertNotIn(
            "/volume1/搶救災害硬碟/救災行車紀錄器:/data/disaster-records",
            compose_text,
        )

    def test_nas_package_includes_default_compose_filename(self) -> None:
        build_script = (PROJECT_ROOT / "scripts" / "build_nas_package.ps1").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            'Copy-FileToOutput -Source $compose -RelativePath "compose.yaml"',
            build_script,
        )


if __name__ == "__main__":
    unittest.main()
