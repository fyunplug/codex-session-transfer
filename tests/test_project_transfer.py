import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile


class ProjectTransferTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('project_test_transfer', Path(__file__).parents[1] / 'scripts/session_transfer.py')
        self.t = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.t)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / 'source'
        (self.project / 'software/resources/app').mkdir(parents=True)
        (self.project / 'app').mkdir()
        (self.project / 'software/program.exe').write_bytes(b'MZ' + b'X' * 10000)
        (self.project / 'software/resources/app/main.js').write_text('console.log(1)')
        (self.project / 'app/main.js').write_text('console.log(1)')
        (self.project / '.env').write_text('PRIVATE_CREDENTIAL')
        self.archive = self.root / 'session.project.zip'

    def build(self):
        return self.t.project_transfer.export_project(self.project, self.archive, self.t.Sanitizer([]), self.t.session_files)

    def test_project_preserves_runtime_tree_and_edits(self):
        with patch.object(self.t.session_files, 'FILE_LIMIT', 100):
            desc = self.build()
        self.assertEqual(desc['files'], 3)
        session = self.root / 'session.zip'
        target = self.root / 'target'
        target.mkdir()
        first = self.t.project_transfer.restore_project(session, desc, target, self.t.session_files)
        runtime = Path(first['root']) / 'software/program.exe'
        self.assertEqual(runtime.read_bytes(), (self.project / 'software/program.exe').read_bytes())
        self.assertFalse((Path(first['root']) / '.env').exists())
        edited = Path(first['root']) / 'app/main.js'
        edited.write_text('edited')
        again = self.t.project_transfer.restore_project(session, desc, target, self.t.session_files)
        self.assertEqual(first['root'], again['root'])
        self.assertEqual(edited.read_text(), 'edited')
        self.assertEqual(first['path_map'][(self.project / 'app/main.js').as_posix()], edited.as_posix())

    def test_missing_or_tampered_companion_is_rejected(self):
        desc = self.build()
        original = self.archive.read_bytes()
        self.archive.unlink()
        with self.assertRaisesRegex(ValueError, 'Missing project companion'):
            self.t.project_transfer.restore_project(self.root / 'session.zip', desc, self.root, self.t.session_files)
        self.archive.write_bytes(original + b'tampered')
        with self.assertRaisesRegex(ValueError, 'SHA256 mismatch'):
            self.t.project_transfer.restore_project(self.root / 'session.zip', desc, self.root, self.t.session_files)
        self.assertFalse((self.root / 'session-projects').exists())

    def test_zip_traversal_and_symlink_destination_are_rejected(self):
        desc = self.build()
        with zipfile.ZipFile(self.archive, 'a') as z:
            z.writestr('../escape', b'bad')
        with self.assertRaises(ValueError):
            self.t.project_transfer.verify_project(self.archive, None, self.t.session_files)
        self.archive.unlink()
        desc = self.build()
        target = self.root / 'target'
        target.mkdir()
        outside = self.root / 'outside'
        outside.mkdir()
        try:
            (target / 'session-projects').symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest('Symlinks are unavailable')
        with self.assertRaises(ValueError):
            self.t.project_transfer.restore_project(self.root / 'session.zip', desc, target, self.t.session_files)
        self.assertEqual(list(outside.iterdir()), [])

    def test_native_session_export_binds_companion_and_rejects_missing_on_restore(self):
        home = self.root / 'home'
        sessions = home / 'sessions'
        sessions.mkdir(parents=True)
        tid = '12345678-1234-4234-8234-123456789abc'
        (sessions / 'source.jsonl').write_text(json.dumps({'type': 'session_meta', 'payload': {'id': tid, 'cwd': str(self.project)}}) + '\n')
        archive = self.t.export_session(home, tid, self.root / 'exports', project_root=self.project, single_file=False)
        report = self.t.verify_archive(archive)
        descriptor = report['manifest']['project_bundle']
        self.assertEqual(descriptor['files'], 3)
        (archive.parent / descriptor['filename']).unlink()
        with self.assertRaisesRegex(ValueError, 'Missing project companion'):
            self.t.restore_session_files(archive, report, self.root)
