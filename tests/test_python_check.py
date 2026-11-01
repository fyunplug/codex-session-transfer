import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


INSTALLER = Path(__file__).parents[1] / 'scripts' / 'install-skill.ps1'


@unittest.skipUnless(os.name == 'nt' and shutil.which('powershell.exe'), 'Windows installer check')
class PythonCheckTests(unittest.TestCase):
    def check(self, candidates):
        self.assertTrue(INSTALLER.is_file(), 'Python-aware installer is missing')
        quote = lambda value: "'" + str(value).replace("'", "''") + "'"
        command = '& ' + quote(INSTALLER) + ' -CheckOnly -PythonCandidates @(' + ','.join(map(quote, candidates)) + ')'
        result = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'RemoteSigned', '-Command', command],
                                text=True, capture_output=True, encoding='utf-8', timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout.lstrip('\ufeff'))

    def test_installed_python_produces_no_install_instruction(self):
        report = self.check([sys.executable])
        self.assertTrue(report['Found'])
        self.assertNotIn('InstallCommand', report)

    def test_missing_python_produces_install_instruction_without_installing(self):
        report = self.check([])
        self.assertFalse(report['Found'])
        self.assertIn('Python.Python.3.13', report['InstallCommand'])

    def test_installer_copies_complete_skill_and_keeps_previous_version_backup(self):
        quote = lambda value: "'" + str(value).replace("'", "''") + "'"
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'skills' / 'codex-session-transfer'
            for _ in range(2):
                command = '& ' + quote(INSTALLER) + ' -Destination ' + quote(target) + ' -PythonCandidates @(' + quote(sys.executable) + ')'
                result = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'RemoteSigned', '-Command', command],
                                        text=True, capture_output=True, encoding='utf-8', timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn('winget install', result.stdout)
            self.assertTrue((target / 'SKILL.md').is_file())
            self.assertTrue((target / 'scripts/session_files.py').is_file())
            self.assertTrue((target / 'install-skill.cmd').is_file())
            self.assertEqual(len(list((Path(tmp) / 'skill-install-backups').glob('codex-session-transfer-*'))), 1)
            self.assertEqual([p.name for p in target.parent.iterdir()], ['codex-session-transfer'])


if __name__ == '__main__':
    unittest.main()
