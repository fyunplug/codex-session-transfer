import os
import base64
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).parents[1]
LAUNCHERS = ('install-skill', 'import-session', 'check-environment')


@unittest.skipUnless(os.name == 'nt' and shutil.which('powershell.exe'), 'Windows launcher check')
class LauncherTests(unittest.TestCase):
    def launch(self, name, body, downloaded=False):
        with tempfile.TemporaryDirectory(prefix='session launcher with spaces ') as tmp:
            root = Path(tmp)
            launcher = root / (name + '.cmd')
            shutil.copyfile(ROOT / launcher.name, launcher)
            scripts = root / 'scripts'
            scripts.mkdir()
            entry = scripts / (name + '.ps1')
            entry.write_text(body, encoding='utf-8')
            if downloaded:
                Path(str(entry) + ':Zone.Identifier').write_text('[ZoneTransfer]\nZoneId=3\n')
            archive = root / 'archive with spaces.zip'
            command = f'"{os.environ["COMSPEC"]}" /d /s /c ""{launcher}" "{archive}""'
            result = subprocess.run(command, input='\n', text=True, capture_output=True,
                                    errors='replace', timeout=30)
            return result, archive

    def test_success_and_archive_argument_with_spaces(self):
        for name in LAUNCHERS:
            with self.subTest(name=name):
                result, archive = self.launch(name, "param([string]$Archive)\nWrite-Output 'LAUNCHER_RAN'\nWrite-Output ([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Archive)))\nexit 0\n")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('LAUNCHER_RAN', result.stdout)
                if name in ('import-session', 'check-environment'):
                    self.assertIn(base64.b64encode(str(archive).encode('utf-8')).decode('ascii'), result.stdout)

    def test_entry_failure_keeps_its_exit_code_after_pause(self):
        for name in LAUNCHERS:
            with self.subTest(name=name):
                result, _ = self.launch(name, "Write-Output 'EXPECTED_FAILURE'\nexit 7\n")
                self.assertEqual(result.returncode, 7, result.stdout + result.stderr)

    def test_downloaded_unsigned_entry_is_blocked_with_recovery_instructions(self):
        for name in LAUNCHERS:
            with self.subTest(name=name):
                result, _ = self.launch(name, "Write-Output 'SHOULD_NOT_RUN'\nexit 0\n", downloaded=True)
                output = result.stdout + result.stderr
                self.assertNotEqual(result.returncode, 0, output)
                self.assertNotIn('SHOULD_NOT_RUN', output)
                self.assertIn('Unblock', output)
                self.assertIn('ZIP', output)
                self.assertIn('extract', output)


if __name__ == '__main__':
    unittest.main()
