import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import zipfile


SCRIPT = Path(__file__).parents[1] / 'scripts' / 'session_transfer.py'


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.is_file(), 'Reusable transfer helper is missing')
        spec = importlib.util.spec_from_file_location('transfer', SCRIPT)
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / 'codex'
        self.sessions = self.home / 'sessions' / '2026' / '10' / '21'
        self.sessions.mkdir(parents=True)
        self.tid = '11111111-1111-4111-8111-111111111111'
        self.src = self.sessions / ('rollout-' + self.tid + '.jsonl')
        self.records = [
            {'type': 'session_meta', 'payload': {'id': self.tid, 'cwd': str(self.root),
             'cli_version': 'test-version', 'creator_account_id': 'acct-private-123'}},
            {'type': 'response_item', 'payload': {'role': 'user', 'content': [
                {'text': 'Continue work. sk-proj-' + 'A' * 40},
                {'text': json.dumps({'refresh_token': 'private-refresh-value'})}]}},
        ]
        self.src.write_bytes(b''.join((json.dumps(r) + '\n').encode() for r in self.records) + b'{"partial":')
        (self.home / 'auth.json').write_text('NEVER_EXPORT_THIS')
        db = sqlite3.connect(self.home / 'state_9.sqlite')
        db.execute('CREATE TABLE threads (id TEXT, rollout_path TEXT, cwd TEXT, title TEXT, creator_account_id TEXT)')
        db.execute('INSERT INTO threads VALUES (?,?,?,?,?)', (self.tid, str(self.src), str(self.root), 'Example', 'acct-private-123'))
        db.execute('INSERT INTO threads VALUES (?,?,?,?,?)', ('other-thread', 'OTHER_THREAD_PRIVATE', '', '', ''))
        db.commit()
        db.close()

    def test_export_redacts_secrets_preserves_source_and_verifies(self):
        before = self.src.read_bytes()
        archive = self.mod.export_session(self.home, self.tid, self.root / 'exports')
        report = self.mod.verify_archive(archive)
        self.assertEqual(report['thread_id'], self.tid)
        self.assertEqual(self.src.read_bytes(), before)
        with zipfile.ZipFile(archive) as z:
            contents = b'\n'.join(z.read(n) for n in z.namelist()).decode()
            for secret in ('acct-private-123', 'private-refresh-value', 'sk-proj-' + 'A' * 40,
                           'NEVER_EXPORT_THIS', 'OTHER_THREAD_PRIVATE'):
                self.assertNotIn(secret, contents)
            manifest = json.loads(z.read('manifest.json'))
            self.assertEqual(manifest['snapshot']['record_count'], 2)
            self.assertGreater(manifest['snapshot']['discarded_tail_bytes'], 0)
            self.assertEqual(manifest['versions']['cli'], 'test-version')
            self.assertEqual(manifest['source_path'], str(self.src))
            self.assertEqual(manifest['metadata_sources'][0]['rows'], 1)

    def test_ambiguous_or_incorrect_identity_never_selects_newest(self):
        with self.assertRaises(ValueError):
            self.mod.find_session(self.home, 'different-id')
        duplicate = self.sessions / 'duplicate.jsonl'
        duplicate.write_bytes(self.src.read_bytes())
        with self.assertRaises(ValueError):
            self.mod.find_session(self.home, self.tid)

    def test_tampering_and_zip_traversal_are_rejected(self):
        archive = self.mod.export_session(self.home, self.tid, self.root / 'exports')
        with zipfile.ZipFile(archive) as z:
            entries = {n: z.read(n) for n in z.namelist()}
        for change in ('tamper', 'traversal'):
            bad = self.root / (change + '.zip')
            revised = dict(entries)
            if change == 'tamper':
                revised['manifest.json'] = b'{}'
            else:
                revised['../escape.txt'] = b'escape'
            with zipfile.ZipFile(bad, 'w') as z:
                for n, data in revised.items():
                    z.writestr(n, data)
            with self.assertRaises(ValueError):
                self.mod.verify_archive(bad)
        self.assertFalse((self.root / 'escape.txt').exists())

    def test_stage_import_only_writes_new_staging_directory(self):
        archive = self.mod.export_session(self.home, self.tid, self.root / 'exports')
        original = self.src.read_bytes()
        destination = self.root / 'staged'
        self.mod.stage_import(archive, destination)
        self.assertTrue((destination / 'manifest.json').is_file())
        self.assertEqual(self.src.read_bytes(), original)
        with self.assertRaises((ValueError, FileExistsError)):
            self.mod.stage_import(archive, destination)

    def test_short_password_generic_token_and_nested_identity_are_redacted(self):
        values = [{'content': json.dumps({'creator_user_id': 'nested-private-id', 'token': 'opaque'})},
                  {'text': 'nested-private-id password=abc token=opaque'}]
        sanitizer = self.mod.Sanitizer(values)
        cleaned = json.dumps(sanitizer.clean(values))
        for secret in ('nested-private-id', 'opaque', 'abc'):
            self.assertNotIn(secret, cleaned)

    def test_conflicting_environment_ids_refuse_default_export(self):
        with patch.dict(os.environ, {'CODEX_THREAD_ID': self.tid, 'CODEX_SESSION_ID': 'different-id'}):
            with patch('sys.argv', ['session_transfer.py', '--home', str(self.home), 'export', '--output', str(self.root / 'exports')]):
                self.assertEqual(self.mod.main(), 1)
        self.assertFalse((self.root / 'exports').exists())

    def test_export_materializes_parent_history_at_the_recorded_fork_boundary(self):
        parent_id = '22222222-2222-4222-8222-222222222222'
        parent = self.sessions / ('rollout-' + parent_id + '.jsonl')
        prefix = b''.join((json.dumps(r) + '\n').encode() for r in [
            {'type': 'session_meta', 'payload': {'id': parent_id}},
            {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': [{'text': 'BEFORE_FORK'}]}}])
        parent.write_bytes(prefix + (json.dumps({'type': 'response_item', 'payload': {'role': 'user', 'content': [{'text': 'LATE_PARENT_MESSAGE'}]}}) + '\n').encode())
        meta = dict(self.records[0]['payload'], history_base={'thread_id': parent_id, 'end_byte_offset': len(prefix), 'end_ordinal_exclusive': 2})
        records = [{'type': 'session_meta', 'payload': meta}, {'type': 'response_item', 'payload': {'role': 'assistant', 'content': [{'text': 'NEW_REPLY'}]}}]
        self.src.write_bytes(b''.join((json.dumps(r) + '\n').encode() for r in records))
        archive = self.mod.export_session(self.home, self.tid, self.root / 'exports')
        with zipfile.ZipFile(archive) as z:
            manifest = json.loads(z.read('manifest.json'))
            data = z.read(manifest['record_path']).decode()
            self.assertIn('BEFORE_FORK', data)
            self.assertIn('NEW_REPLY', data)
            self.assertNotIn('LATE_PARENT_MESSAGE', data)
            self.assertNotIn('history_base', json.loads(data.splitlines()[0])['payload'])
            self.assertEqual(manifest['snapshot']['record_count'], 3)

    def rotated_segments(self):
        old = [dict(self.records[0], ordinal=0), dict(self.records[1], ordinal=1)]
        prefix = b''.join((json.dumps(r) + '\n').encode() for r in old)
        parent = self.sessions / 'previous.jsonl'
        late = {'ordinal': 2, 'type': 'response_item', 'payload': {'role': 'assistant', 'content': [{'text': 'EXCLUDED_LATE_PARENT'}]}}
        parent.write_bytes(prefix + (json.dumps(late) + '\n').encode())
        child = {'ordinal': 2, 'type': 'session_meta', 'payload': dict(self.records[0]['payload'],
            history_base={'thread_id': self.tid, 'end_byte_offset': len(prefix), 'end_ordinal_exclusive': 2})}
        reply = {'ordinal': 3, 'type': 'response_item', 'payload': {'role': 'assistant', 'content': [{'text': 'NEW_SEGMENT_REPLY'}]}}
        self.src.write_bytes(b''.join((json.dumps(r) + '\n').encode() for r in [child, reply]))
        return parent, prefix

    def compress(self, data):
        import ctypes
        try:
            lib = self.mod.session_storage.zstd_library()
        except ValueError as exc:
            self.skipTest(str(exc))
        lib.ZSTD_compressBound.argtypes = [ctypes.c_size_t]
        lib.ZSTD_compressBound.restype = ctypes.c_size_t
        lib.ZSTD_compress.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
        lib.ZSTD_compress.restype = ctypes.c_size_t
        output = ctypes.create_string_buffer(lib.ZSTD_compressBound(len(data)))
        size = lib.ZSTD_compress(output, len(output), data, len(data), 3)
        self.assertFalse(lib.ZSTD_isError(size))
        return output.raw[:size]

    def test_rotated_same_id_materializes_exact_prefix_without_later_parent(self):
        parent, prefix = self.rotated_segments()
        before = self.src.read_bytes(), parent.read_bytes()
        archive = self.mod.export_session(self.home, self.tid, self.root / 'exports')
        with zipfile.ZipFile(archive) as z:
            manifest = json.loads(z.read('manifest.json'))
            data = z.read(manifest['record_path']).decode()
            self.assertIn('NEW_SEGMENT_REPLY', data)
            self.assertNotIn('EXCLUDED_LATE_PARENT', data)
            self.assertEqual(manifest['snapshot']['record_count'], 3)
            self.assertEqual(len(manifest['snapshot']['lineage_sources']), 2)
        self.assertEqual(before, (self.src.read_bytes(), parent.read_bytes()))

    def test_compressed_same_id_parent_is_found_and_bounded(self):
        parent, prefix = self.rotated_segments()
        compressed = parent.with_suffix('.jsonl.zst')
        compressed.write_bytes(self.compress(parent.read_bytes()))
        parent.unlink()
        archive = self.mod.export_session(self.home, self.tid, self.root / 'exports')
        with zipfile.ZipFile(archive) as z:
            manifest = json.loads(z.read('manifest.json'))
            self.assertEqual(manifest['snapshot']['lineage_sources'][1]['encoding'], 'zstd')
            self.assertEqual(manifest['snapshot']['lineage_sources'][1]['bytes'], len(prefix))
            self.assertNotIn('EXCLUDED_LATE_PARENT', z.read(manifest['record_path']).decode())

    def test_wrong_rotated_ordinal_and_duplicate_parent_are_rejected(self):
        parent, _ = self.rotated_segments()
        duplicate = parent.with_name('ambiguous.jsonl')
        duplicate.write_bytes(parent.read_bytes())
        with self.assertRaises(ValueError):
            self.mod.export_session(self.home, self.tid, self.root / 'exports')
        duplicate.unlink()
        rows = [json.loads(l) for l in self.src.read_bytes().splitlines()]
        rows[0]['payload']['history_base']['end_ordinal_exclusive'] = 100
        self.src.write_bytes(b''.join((json.dumps(r) + '\n').encode() for r in rows))
        with self.assertRaises(ValueError):
            self.mod.export_session(self.home, self.tid, self.root / 'exports')
        self.assertFalse((self.root / 'exports').exists())

    def test_compressed_only_source_and_truncated_frame(self):
        raw = self.src.read_bytes().split(b'{"partial":')[0]
        compressed = self.src.with_suffix('.jsonl.zst')
        compressed.write_bytes(self.compress(raw))
        self.src.unlink()
        archive = self.mod.export_session(self.home, self.tid, self.root / 'exports')
        self.assertTrue(self.mod.verify_archive(archive)['manifest']['record_path'].endswith('.jsonl'))
        compressed.write_bytes(compressed.read_bytes()[:-2])
        with self.assertRaises(ValueError):
            self.mod.session_storage.read_session(compressed)

    def test_compressed_streaming_and_decoded_size_limit(self):
        raw = b'A' * (1024 * 1024)
        path = self.sessions / 'stream.jsonl.zst'
        path.write_bytes(self.compress(raw))
        self.assertEqual(self.mod.session_storage.read_session(path), raw)
        with patch.object(self.mod.session_storage, 'LIMIT', 1000):
            with self.assertRaises(ValueError):
                self.mod.session_storage.read_session(path)


if __name__ == '__main__':
    unittest.main()
