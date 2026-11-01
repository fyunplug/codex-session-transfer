import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).parents[1] / 'scripts' / 'native_import.py'


class NativeRecoveryTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('native_recovery_test', SCRIPT)
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home, self.cwd = self.root / 'home', self.root / 'project'
        self.home.mkdir()
        self.cwd.mkdir()
        self.output = self.root / 'receipts'
        self.executable = self.root / 'unused-codex.exe'
        self.executable.write_bytes(b'not executed')
        self.report = {'sha256': 'a' * 64, 'manifest': {}}
        key = self.mod.transfer.sha(self.mod.transfer.encoded([
            self.report['sha256'], str(self.home.resolve()), str(self.cwd.resolve()), 'path-rewrite-v2']))[:20]
        self.work = self.output / key
        self.generated = self.work / 'portable' / 'sessions' / 'restored.jsonl'
        self.generated.parent.mkdir(parents=True)
        self.generated.write_bytes(b'complete native rollout\n')
        self.destination = self.home / 'sessions' / 'restored.jsonl'
        self.receipt_path = self.work / 'receipt.json'
        self.receipt = {
            'thread_id': 'new-thread-id', 'source_thread_id': 'old-thread-id', 'title': 'Restored',
            'cwd': str(self.cwd), 'rollout_path': str(self.destination),
            'engine_rollout': str(self.generated), 'source_archive_sha256': self.report['sha256'],
            'source_turn_count': 0,
            'source_items_digest': self.mod.transfer.sha(self.mod.transfer.encoded([])),
            'status': 'prepared',
        }
        self.mod.save_receipt(self.receipt_path, self.receipt)
        test = self

        class FakeServer:
            def __init__(self, *_):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

            def call(self, method, params):
                if method == 'thread/list':
                    return {'data': [{'id': test.receipt['thread_id']}]}
                if method == 'thread/read':
                    return {'thread': {'id': test.receipt['thread_id'], 'cwd': str(test.cwd)}}
                if method == 'thread/goal/get':
                    return {'goal': None}
                if method == 'thread/resume':
                    return {'thread': {'id': test.receipt['thread_id']}}
                if method in ('thread/turns/list', 'thread/items/list'):
                    return {'data': []}
                return {}

        self.server = FakeServer
        for patcher in (
            patch.object(self.mod.transfer, 'resolve_archive', side_effect=lambda p: p),
            patch.object(self.mod.transfer, 'verify_archive', return_value=self.report),
            patch.object(self.mod.transfer, 'restore_session_files', return_value={'files': [], 'path_map': {}}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def restore(self, server=None):
        with patch.object(self.mod, 'AppServer', server or self.server):
            return self.mod.restore_native(self.root / 'unused.zip', self.home, self.cwd,
                                           self.output, self.executable)

    def test_interrupted_index_backup_is_rebuilt_before_installation(self):
        index = self.home / 'session_index.jsonl'
        expected = b'{"id":"existing-thread"}\n'
        index.write_bytes(expected)
        original_write = Path.write_bytes

        def fail_backup_write(path, content):
            if path.name == index.name and self.home / 'session-transfer-backups' in path.parents:
                original_write(path, content[:3])
                raise OSError('simulated disk full while backing up the index')
            return original_write(path, content)

        with patch.object(Path, 'write_bytes', fail_backup_write):
            with self.assertRaisesRegex(OSError, 'simulated disk full'):
                self.restore()
        self.assertFalse(self.destination.exists())
        result = self.restore()
        self.assertEqual(result['status'], 'complete')
        self.assertEqual((Path(result['backup_path']) / index.name).read_bytes(), expected)

    def test_existing_incomplete_backup_is_preserved_and_rebuilt(self):
        index = self.home / 'session_index.jsonl'
        index.write_bytes(b'complete index\n')
        backup = self.home / 'session-transfer-backups' / self.work.name
        backup.mkdir(parents=True)
        (backup / index.name).write_bytes(b'partial')
        result = self.restore()
        self.assertEqual((Path(result['backup_path']) / index.name).read_bytes(), index.read_bytes())
        self.assertTrue(any(path.read_bytes() == b'partial'
                            for path in backup.parent.rglob(index.name)))

    def test_complete_import_can_be_found_on_second_native_list_page(self):
        self.receipt['status'] = 'complete'
        self.mod.save_receipt(self.receipt_path, self.receipt)
        test = self

        class PagedServer(self.server):
            def call(self, method, params):
                if method == 'thread/list':
                    if params.get('cursor') == 'page-two':
                        return {'data': [{'id': test.receipt['thread_id']}]}
                    return {'data': [{'id': 'newer-thread'}], 'nextCursor': 'page-two'}
                return super().call(method, params)

        self.assertEqual(self.restore(PagedServer)['status'], 'complete')

    def test_interrupted_rollout_write_leaves_no_partial_target_and_can_retry(self):
        original_open = Path.open
        test = self

        class InterruptedWriter:
            def __init__(self, path, *args, **kwargs):
                self.file = original_open(path, *args, **kwargs)

            def __enter__(self):
                self.file.__enter__()
                return self

            def __exit__(self, *args):
                return self.file.__exit__(*args)

            def write(self, value):
                self.file.write(value[:3])
                raise OSError('simulated interrupted rollout write')

        def fail_rollout_open(path, *args, **kwargs):
            mode = args[0] if args else kwargs.get('mode', 'r')
            if path.parent == test.destination.parent and mode == 'xb':
                return InterruptedWriter(path, *args, **kwargs)
            return original_open(path, *args, **kwargs)

        with patch.object(Path, 'open', fail_rollout_open):
            with self.assertRaisesRegex(OSError, 'simulated interrupted rollout'):
                self.restore()
        self.assertFalse(self.destination.exists())
        result = self.restore()
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(self.destination.read_bytes(), self.generated.read_bytes())

    def main_output(self, archive, *extra):
        arguments = ['native_import.py', str(archive), '--home', str(self.home),
                     '--cwd', str(self.cwd), *extra]
        with patch('sys.argv', arguments), patch('builtins.print'), \
                patch.object(self.mod, 'restore_native', return_value={}) as restore:
            self.assertEqual(self.mod.main(), 0)
        return restore.call_args.args[3]

    def test_default_receipts_stay_in_target_home_when_archive_moves(self):
        expected = self.home / 'session-transfer-imports'
        self.assertEqual(self.main_output(self.root / 'downloads' / 'snapshot.zip'), expected)
        self.assertEqual(self.main_output(self.root / 'elsewhere' / 'snapshot.zip'), expected)

    def test_default_output_reuses_legacy_receipt_for_same_archive_home_and_project(self):
        archive = self.root / 'downloads' / 'snapshot.zip'
        legacy = archive.parent / 'session-imports'
        receipt = legacy / self.work.name / 'receipt.json'
        receipt.parent.mkdir(parents=True)
        self.mod.save_receipt(receipt, self.receipt)
        self.assertEqual(self.main_output(archive), legacy)

    def test_explicit_output_still_takes_precedence(self):
        explicit = self.root / 'chosen-receipts'
        self.assertEqual(self.main_output(self.root / 'snapshot.zip', '--output', str(explicit)), explicit)


if __name__ == '__main__':
    unittest.main()
