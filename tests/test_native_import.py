import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
import zipfile


SCRIPT = Path(__file__).parents[1] / 'scripts' / 'native_import.py'


class NativeImportTests(unittest.TestCase):
    def load(self):
        self.assertTrue(SCRIPT.is_file(), 'Native restoration helper is missing')
        spec = importlib.util.spec_from_file_location('native_import', SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_missing_executable_never_changes_target_home(self):
        mod = self.load()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / 'target-home'
            with self.assertRaises((ValueError, FileNotFoundError)):
                mod.restore_native(root / 'missing.zip', home, root, root / 'receipts', root / 'missing.exe')
            self.assertFalse(home.exists())

    @unittest.skipUnless(os.environ.get('NATIVE_TEST_CODEX') and os.environ.get('NATIVE_TEST_ZIP'),
                         'Optional real Codex engine integration test')
    def test_real_engine_restores_history_without_prompt_and_is_idempotent(self):
        mod = self.load()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / 'target-home'
            cwd = root / 'working project with spaces'
            cwd.mkdir()
            archive = Path(os.environ['NATIVE_TEST_ZIP'])
            original = archive.read_bytes()
            first = mod.restore_native(archive, home, cwd, root / 'receipts', Path(os.environ['NATIVE_TEST_CODEX']))
            self.assertNotEqual(first['thread_id'], first['source_thread_id'])
            self.assertTrue(first['native_import_applied'])
            self.assertTrue(first['history_verified'])
            self.assertTrue(first['resume_verified'])
            self.assertTrue(first['listed_in_app_server'])
            self.assertEqual(first['chat_messages_sent'], 0)
            if first.get('restored_project'):
                self.assertEqual(Path(first['cwd']), Path(first['restored_project']['root']))
                self.assertTrue(Path(first['cwd']).is_relative_to(cwd / 'session-projects'))
            else:
                self.assertEqual(Path(first['cwd']), cwd)
            self.assertTrue(Path(first['rollout_path']).is_relative_to(home))
            self.assertEqual(archive.read_bytes(), original)
            with zipfile.ZipFile(mod.transfer.resolve_archive(archive)) as z:
                manifest = json.loads(z.read('manifest.json'))
                assets = manifest.get('attachments', [])
                self.assertEqual(len(first.get('restored_files', [])), len(assets))
                for asset, restored in zip(assets, first.get('restored_files', [])):
                    self.assertEqual(Path(restored['path']).read_bytes(), z.read(asset['archive_path']))
                    self.assertIn(Path(restored['path']).as_posix(), Path(first['rollout_path']).read_text(encoding='utf-8'))
            if assets:
                edited = Path(first['restored_files'][0]['path'])
                edited.write_bytes(b'Edited after native import')
            before_ids = {meta['id'] for _, meta in mod.transfer.candidates(home)}
            again = mod.restore_native(archive, home, cwd, root / 'receipts', Path(os.environ['NATIVE_TEST_CODEX']))
            self.assertEqual(again['thread_id'], first['thread_id'])
            self.assertEqual({meta['id'] for _, meta in mod.transfer.candidates(home)}, before_ids)
            # Current engines may create an empty guardian-review auxiliary thread on resume.
            for path, meta in mod.transfer.candidates(home):
                if meta['id'] != first['thread_id']:
                    self.assertEqual(meta.get('thread_source'), 'guardian_review')
                    self.assertFalse(mod.visible_messages(mod.transfer.session_storage.read_session(path)))
            if assets:
                self.assertEqual(edited.read_bytes(), b'Edited after native import')


if __name__ == '__main__':
    unittest.main()
