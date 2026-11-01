import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import zipfile


class ArchiveValidationTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).parents[1] / 'scripts/session_transfer.py'
        spec = importlib.util.spec_from_file_location('validate_test', path)
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.tid = '11111111-1111-4111-8111-111111111111'
        self.manifest = {'format': 'codex-session-transfer-v2', 'thread_id': self.tid, 'record_path': 'sessions/session.jsonl',
                         'snapshot': {'record_count': 1}, 'attachments': []}

    def write(self, *, manifest=None, records=None, checks=None):
        records = [{'type': 'session_meta', 'payload': {'id': self.tid}}] if records is None else records
        files = {'manifest.json': json.dumps(self.manifest if manifest is None else manifest).encode(),
                 'sessions/session.jsonl': b''.join((json.dumps(r) + '\n').encode() for r in records)}
        sums = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
        files['SHA256SUMS.json'] = json.dumps(sums if checks is None else checks).encode()
        archive = self.root / 'archive.zip'
        with zipfile.ZipFile(archive, 'w') as z:
            for name, data in files.items():
                z.writestr(name, data)
        return archive

    def test_wrong_json_shapes_fail_with_controlled_validation_errors(self):
        scenarios = [dict(checks=['manifest.json', 'sessions/session.jsonl']), dict(manifest=[]), dict(records=[7]),
                     dict(manifest=dict(self.manifest, snapshot=[])),
                     dict(manifest=dict(self.manifest, attachments=[{'id': 7, 'archive_path': 42}]))]
        for kwargs in scenarios:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    self.mod.verify_archive(self.write(**kwargs))

    def test_empty_or_invalid_whole_archive_checksum_is_rejected_cleanly(self):
        archive = self.write()
        for value in ('', 'not-a-sha256'):
            with self.subTest(value=value):
                archive.with_suffix('.zip.sha256').write_text(value, encoding='utf-8')
                with self.assertRaises(ValueError):
                    self.mod.verify_archive(archive)

    def test_windows_reserved_archive_names_are_rejected_before_extraction(self):
        archive = self.write()
        with zipfile.ZipFile(archive) as z:
            files = {n: z.read(n) for n in z.namelist()}
        files['metadata/CON.txt'] = b'invalid Windows name'
        files['SHA256SUMS.json'] = json.dumps({n: hashlib.sha256(v).hexdigest() for n, v in files.items() if n != 'SHA256SUMS.json'}).encode()
        with zipfile.ZipFile(archive, 'w') as z:
            for name, data in files.items():
                z.writestr(name, data)
        with self.assertRaises(ValueError):
            self.mod.verify_archive(archive)
