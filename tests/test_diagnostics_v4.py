import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


class DiagnosticTests(unittest.TestCase):
    def load(self):
        spec = importlib.util.spec_from_file_location('diagnostic_test', Path(__file__).parents[1] / 'scripts/session_transfer.py')
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_missing_codex_diagnostic_is_clear_and_does_not_create_home(self):
        mod = self.load()
        self.assertTrue(hasattr(mod, 'environment_report'), 'Read-only environment diagnostic is missing')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = mod.environment_report(root / 'not-created-home', root / 'missing-codex.exe')
            self.assertFalse(report['runtime_ready'])
            self.assertTrue(report['python']['ready'])
            self.assertFalse(report['codex']['ready'])
            self.assertTrue(report['codex']['message'])
            self.assertFalse((root / 'not-created-home').exists())
