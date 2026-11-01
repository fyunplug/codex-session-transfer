import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import zipfile


class SingleBundleTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('single_bundle_test', Path(__file__).parents[1] / 'scripts/session_transfer.py')
        self.t = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.t)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / 'home'
        (self.home / 'sessions').mkdir(parents=True)
        self.project = self.root / 'source'
        self.project.mkdir()
        (self.project / 'main.py').write_text('print(1)')
        self.tid = '12345678-1234-4234-8234-123456789abc'
        rows = [{'type': 'session_meta', 'payload': {'id': self.tid, 'cwd': str(self.project)}},
                {'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant',
                  'content': [{'type': 'output_text', 'text': (self.project / 'main.py').as_posix()}]}}]
        (self.home / 'sessions/source.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in rows))

    def test_default_export_is_one_zip_and_can_be_renamed_and_restored(self):
        archive = self.t.export_session(self.home, self.tid, self.root / 'exports', project_root=self.project)
        self.assertEqual(len(list(archive.parent.glob('*.zip'))), 1)
        moved = self.root / 'renamed.zip'
        archive.rename(moved)
        r = self.t.verify_archive(moved)
        target = self.root / 'destination'
        target.mkdir()
        restored = self.t.restore_session_files(moved, r, target)
        p = Path(restored['project']['root']) / 'main.py'
        self.assertEqual(p.read_text(), 'print(1)')
        p.write_text('edited')
        self.t.restore_session_files(moved, r, target)
        self.assertEqual(p.read_text(), 'edited')

    def test_corrupted_nested_zip_and_unsafe_members_are_rejected(self):
        archive = self.t.export_session(self.home, self.tid, self.root / 'exports', project_root=self.project)
        with zipfile.ZipFile(archive) as z:
            original = {n: z.read(n) for n in z.namelist()}
        for mode in ('tamper', 'path'):
            entries = dict(original)
            if mode == 'tamper':
                name = next(n for n in entries if n.endswith('.project.zip'))
                entries[name] = b'X' * len(entries[name])
            else:
                name = next(n for n in entries if n.endswith('.project.zip'))
                entries['../escape.zip'] = entries.pop(name)
            bad = self.root / (mode + '.zip')
            with zipfile.ZipFile(bad, 'w') as z:
                for n, data in entries.items():
                    z.writestr(n, data)
            with self.assertRaises(ValueError):
                self.t.verify_archive(bad)
        self.assertFalse((self.root / 'escape.zip').exists())

    def test_optional_outer_checksum_is_checked(self):
        archive = self.t.export_session(self.home, self.tid, self.root / 'exports', project_root=self.project)
        archive.with_suffix('.zip.sha256').write_text('0' * 64)
        with self.assertRaisesRegex(ValueError, 'outer SHA256 mismatch'):
            self.t.verify_archive(archive)
        archive.with_suffix('.zip.sha256').unlink()
        self.assertTrue(self.t.verify_archive(archive)['verified'])
