import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENTRY_BIND = "${TASK_ENTRY_LAN_IP:?請設定實際NAS區網IP}"


class NasEntryComposeTests(unittest.TestCase):
    def test_override_only_extends_existing_app_service(self) -> None:
        text = (PROJECT_ROOT / "compose.entry.yml").read_text(encoding="utf-8")

        self.assertEqual(re.findall(r"^\S[^\n]*:", text, re.MULTILINE), ["services:"])
        self.assertEqual(re.findall(r"^  (\w+):", text, re.MULTILINE), ["app"])
        self.assertEqual(
            re.findall(r"^    (\w+):", text, re.MULTILINE),
            ["environment", "ports"],
        )
        base = (PROJECT_ROOT / "compose.nas.yml").read_text(encoding="utf-8")
        self.assertIn('      - "8080:8080"', base)
        self.assertIn("      - ./:/app", base)
        self.assertIn("      - .env", base)

    def test_entry_listener_requires_an_explicit_lan_address(self) -> None:
        text = (PROJECT_ROOT / "compose.entry.yml").read_text(encoding="utf-8")

        self.assertIn('      TASK_ENTRY_PORT: "8091"', text)
        self.assertIn(f'      TASK_ENTRY_LAN_IP: "{ENTRY_BIND}"', text)
        self.assertIn(f'      TASK_ENTRY_URL: "http://{ENTRY_BIND}:8091/"', text)
        self.assertEqual(
            re.findall(r'^      - "([^"]+)"$', text, re.MULTILINE),
            [f"{ENTRY_BIND}:8091:8091"],
        )
        self.assertNotIn("0.0.0.0", text)
        self.assertNotIn("8080", text)


@unittest.skipUnless(shutil.which("powershell.exe"), "NAS packaging requires PowerShell")
class NasEntryPackageTests(unittest.TestCase):
    def test_real_build_includes_optional_profile_and_excludes_private_env(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ambulance-nas-entry-package-") as tmp:
            project = Path(tmp)
            source = project / "fixture-runtime"
            source.mkdir()
            source_files = (
                "app.py", "worker.py", "civilpower.py", "consumables_login.py",
                "disinfect.py", "requirements.txt", ".env.example",
            )
            for name in source_files:
                (source / name).write_text("# fixture public content\n", encoding="utf-8")
            (source / "VERSION.txt").write_text("2026.10.16.0100\n", encoding="utf-8")
            secret = "fixture-only-private-secret-do-not-package"
            for parent in (project, source):
                (parent / ".env").write_text(f"TOKEN={secret}\n", encoding="utf-8")
            for name in ("ambulance_bot", "templates", "static"):
                directory = source / name
                directory.mkdir()
                (directory / "public.txt").write_text("public fixture\n", encoding="utf-8")
                (directory / ".env").write_text(secret, encoding="utf-8")
            for name in ("compose.nas.yml", "compose.entry.yml"):
                shutil.copyfile(PROJECT_ROOT / name, project / name)
            output = project / "UPDATE" / "entry-package"
            env = os.environ.copy()
            result = subprocess.run(
                [
                    "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                    str(PROJECT_ROOT / "scripts" / "build_nas_package.ps1"),
                    "-ProjectRoot", str(project), "-SourceDir", str(source),
                    "-OutputDir", str(output),
                ],
                cwd=project, env=env, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=60, check=False,
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            for name in ("compose.nas.yml", "compose.entry.yml"):
                self.assertEqual((output / name).read_bytes(), (project / name).read_bytes())
            self.assertEqual((output / "compose.yaml").read_bytes(), (project / "compose.nas.yml").read_bytes())
            for path in output.rglob("*"):
                if path.is_file():
                    self.assertNotEqual(path.name, ".env")
                    self.assertNotIn(secret.encode("utf-8"), path.read_bytes(), str(path))
            instructions = (output / "NAS_DEPLOY.txt").read_text(encoding="utf-8-sig")
            self.assertIn("TASK_ENTRY_LAN_IP", instructions)
            self.assertIn("-f compose.yaml -f compose.entry.yml", instructions)
            self.assertIn("docker compose -p ambulance -f compose.yaml -f compose.entry.yml up -d app", instructions)
            self.assertIn("recreates the existing app", instructions)
            self.assertIn("10.30.65.30", instructions)
            self.assertIn("8080", instructions)
            self.assertIn("one app container", instructions)
            self.assertEqual((project / ".env").read_text(encoding="utf-8"), f"TOKEN={secret}\n")
            self.assertEqual((source / ".env").read_text(encoding="utf-8"), f"TOKEN={secret}\n")


if __name__ == "__main__":
    unittest.main()
