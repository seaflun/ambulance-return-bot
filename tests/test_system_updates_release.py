from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify_system_updates.py"
VERSION = "2026.10.13.2245"


def card(version=VERSION, item="修正加油總價即時更新"):
    return (f'<article class="update-card" data-release-version="{version}">'
            f'<h2>更新說明</h2><span>{version}</span><ul><li>{item}</li></ul></article>')


class SystemUpdatesReleaseTests(unittest.TestCase):
    def check_page(self, page, package_page=None, package_version=VERSION):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = root / "system_updates.html"
            template.write_text(page, encoding="utf-8")
            command = [sys.executable, "-X", "utf8", str(SCRIPT), "--version", VERSION,
                       "--template", str(template)]
            if package_page is not None:
                package = root / "package.zip"
                with zipfile.ZipFile(package, "w") as archive:
                    archive.writestr("WinPython_公務電腦使用包/VERSION.txt", package_version)
                    archive.writestr("WinPython_公務電腦使用包/templates/system_updates.html", package_page)
                command += ["--package", str(package)]
            return subprocess.run(command, capture_output=True, text=True, encoding="utf-8")

    def test_accepts_current_visible_notes_and_matching_package(self):
        result = self.check_page(card(), card())
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_rejects_missing_stale_empty_or_hidden_version_notes(self):
        cases = {
            "missing": "<article>舊版更新</article>",
            "stale": card("2026.10.13.1549"),
            "empty": card(item="   "),
            "wrong_order": card("2026.10.13.1549") + card(),
            "duplicate": card() + card(),
            "unlabelled": card().replace(f"<span>{VERSION}</span>", ""),
        }
        for label, page in cases.items():
            with self.subTest(label=label):
                result = self.check_page(page)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("System updates check failed", result.stderr)

    def test_rejects_stale_packaged_page_or_version(self):
        for page, version in [(card("2026.10.13.1549"), VERSION),
                              (card(), "2026.10.13.1549")]:
            with self.subTest(version=version, page=page):
                result = self.check_page(card(), page, version)
                self.assertNotEqual(result.returncode, 0)

    def test_repository_notes_cover_current_version(self):
        result = subprocess.run([sys.executable, "-X", "utf8", str(SCRIPT)], capture_output=True,
                                text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
