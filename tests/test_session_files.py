import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

SCRIPT = Path(__file__).parents[1] / 'scripts' / 'session_transfer.py'


class SessionFileTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('transfer_files_test', SCRIPT)
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / 'codex-home'
        self.home.mkdir()
        self.tid = '11111111-1111-4111-8111-111111111111'
        self.source = self.home / 'sessions' / 'test.jsonl'
        self.source.parent.mkdir()
        self.project = self.root / 'project'
        self.project.mkdir()
        self.note = self.project / '说明 文件.txt'
        self.note.write_text('Editable original\n', encoding='utf-8')
        self.document = self.project / 'report.docx'
        with zipfile.ZipFile(self.document, 'w') as z:
            z.writestr('word/document.xml', '<document>Portable document</document>')
        self.message = f'[note](<{self.note}>) and [doc]({self.document.as_posix()}) and [missing](missing.xlsx)'

    def export(self, text=None):
        records = [
            {'type': 'session_meta', 'payload': {'id': self.tid, 'cwd': str(self.project)}},
            {'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant',
             'content': [{'type': 'output_text', 'text': self.message if text is None else text}]}},
            {'type': 'response_item', 'payload': {'type': 'function_call', 'name': 'exec_command',
             'arguments': json.dumps({'cmd': f'cat {self.note}'})}},
        ]
        self.source.write_bytes(b''.join((json.dumps(r, ensure_ascii=False) + '\n').encode('utf-8') for r in records))
        return self.mod.export_session(self.home, self.tid, self.root / 'exports')

    def test_linked_text_and_office_files_pack_byte_exact_with_missing_report(self):
        archive = self.export()
        report = self.mod.verify_archive(archive)
        assets = report['manifest'].get('attachments', [])
        self.assertEqual(len(assets), 2, 'Linked files must be included, not only historical path strings')
        with zipfile.ZipFile(archive) as z:
            for asset in assets:
                self.assertEqual(z.read(asset['archive_path']), Path(asset['source_path']).read_bytes())
            self.assertIn('FILE-REPORT.txt', z.namelist())
            summary = z.read('FILE-REPORT.txt').decode('utf-8')
            self.assertIn('report.docx', summary)
            self.assertIn('not_found', summary)
        self.assertTrue(any(r['reason'] == 'not_found' for r in report['manifest']['file_report']))

    def test_restore_relinks_messages_only_and_preserves_later_edits(self):
        archive = self.export()
        report = self.mod.verify_archive(archive)
        other = self.root / 'another-computer'
        other.mkdir()
        restored = self.mod.restore_session_files(archive, report, other)
        self.assertEqual(len(restored['files']), 2)
        with zipfile.ZipFile(archive) as z:
            original = z.read(report['manifest']['record_path'])
        changed = self.mod.relink_session(original, restored['path_map'])
        records = [json.loads(line) for line in changed.splitlines()]
        text = records[1]['payload']['content'][0]['text']
        for entry in restored['files']:
            self.assertIn(Path(entry['path']).as_posix(), text)
            self.assertTrue(Path(entry['path']).is_file())
        self.assertEqual(records[2]['payload']['arguments'], json.loads(original.splitlines()[2])['payload']['arguments'])
        first = Path(restored['files'][0]['path'])
        first.write_bytes(b'Changed on the new computer')
        self.mod.restore_session_files(archive, report, other)
        self.assertEqual(first.read_bytes(), b'Changed on the new computer')
        self.assertEqual(self.note.read_text(encoding='utf-8'), 'Editable original\n')

    def test_credentials_are_excluded_and_no_whole_folder_is_copied(self):
        secret = self.project / '.env'
        secret.write_text('PASSWORD=private')
        leaked = self.project / 'notes.txt'
        leaked.write_text('api_key=' + 'sk-proj-' + 'B' * 40)
        unrelated = self.project / 'unrelated.txt'
        unrelated.write_text('Unreferenced')
        archive = self.export(f'[env]({secret}) [unsafe]({leaked}) [ok]({self.note.as_posix().replace(" ", "%20")})')
        report = self.mod.verify_archive(archive)
        self.assertEqual(len(report['manifest'].get('attachments', [])), 1)
        with zipfile.ZipFile(archive) as z:
            raw = b''.join(z.read(n) for n in z.namelist())
            self.assertNotIn(('sk-proj-' + 'B' * 40).encode(), raw)
            self.assertNotIn(b'Unreferenced', raw)
        self.assertEqual(len(report['manifest']['file_report']), 2)

    def test_attachment_manifest_cannot_point_at_session_or_traverse(self):
        archive = self.export()
        with zipfile.ZipFile(archive) as z:
            entries = {n: z.read(n) for n in z.namelist()}
        manifest = json.loads(entries['manifest.json'])
        self.assertTrue(manifest.get('attachments'), 'No attachment support')
        manifest['attachments'][0]['archive_path'] = manifest['record_path']
        entries['manifest.json'] = self.mod.encoded(manifest)
        entries['SHA256SUMS.json'] = self.mod.encoded({n: self.mod.sha(data) for n, data in entries.items() if n != 'SHA256SUMS.json'})
        bad = self.root / 'bad.zip'
        with zipfile.ZipFile(bad, 'w') as z:
            for n, data in entries.items():
                z.writestr(n, data)
        with self.assertRaises(ValueError):
            self.mod.verify_archive(bad)

    def test_parenthesized_and_line_number_links_remain_clickable(self):
        parenthesized = self.project / 'result(1).txt'
        parenthesized.write_text('Another editable file')
        archive = self.export(f'[result]({parenthesized.as_posix()}:12)')
        report = self.mod.verify_archive(archive)
        self.assertEqual(len(report['manifest'].get('attachments', [])), 1)
        target = self.root / 'target'
        target.mkdir()
        restored = self.mod.restore_session_files(archive, report, target)
        with zipfile.ZipFile(archive) as z:
            data = z.read(report['manifest']['record_path'])
        changed = self.mod.relink_session(data, restored['path_map']).decode()
        self.assertIn(Path(restored['files'][0]['path']).as_posix() + ':12', changed)

    def test_relative_link_rebinding_does_not_change_other_links_or_urls(self):
        text = '[report](report.docx) [other](other-report.docx) [remote](https://example.org/report.docx)'
        data = (json.dumps({'type': 'response_item', 'payload': {'role': 'assistant', 'content': [{'text': text}]}}) + '\n').encode()
        changed = self.mod.relink_session(data, {'report.docx': 'C:/target/report.docx'}).decode()
        self.assertIn('[report](C:/target/report.docx)', changed)
        self.assertIn('[other](other-report.docx)', changed)
        self.assertIn('[remote](https://example.org/report.docx)', changed)

    def test_nested_zip_credentials_are_not_carried_as_an_attachment(self):
        inner = io.BytesIO()
        with zipfile.ZipFile(inner, 'w', zipfile.ZIP_DEFLATED) as z:
            z.writestr('auth.json', json.dumps({'refresh_token': 'private-nested-credential'}))
        backup = self.project / 'backup.zip'
        with zipfile.ZipFile(backup, 'w', zipfile.ZIP_DEFLATED) as z:
            z.writestr('nested.zip', inner.getvalue())
        report = self.mod.verify_archive(self.export(f'[backup]({backup.as_posix()})'))
        self.assertEqual(len(report['manifest']['attachments']), 0)
        self.assertEqual(report['manifest']['file_report'][0]['reason'], 'possible_credentials')

    def test_import_into_space_named_project_keeps_markdown_links_clickable(self):
        archive = self.export()
        report = self.mod.verify_archive(archive)
        target = self.root / 'New project with spaces'
        target.mkdir()
        restored = self.mod.restore_session_files(archive, report, target)
        with zipfile.ZipFile(archive) as z:
            changed = self.mod.relink_session(z.read(report['manifest']['record_path']), restored['path_map'])
        messages = [json.loads(line) for line in changed.splitlines()]
        text = messages[1]['payload']['content'][0]['text']
        document = next(item for item in restored['files'] if item['source_path'].endswith('report.docx'))
        self.assertIn('[doc](<' + Path(document['path']).as_posix() + '>)', text)

    def test_relative_references_follow_each_turn_working_directory(self):
        first = self.project / 'report.txt'
        first.write_text('First project')
        second_project = self.root / 'second-project'
        second_project.mkdir()
        second = second_project / 'report.txt'
        second.write_text('Second project')
        def reply():
            return {'type': 'response_item', 'payload': {'role': 'assistant', 'content': [{'text': '[file](report.txt)'}]}}
        records = [{'type': 'session_meta', 'payload': {'id': self.tid, 'cwd': str(self.project)}}, reply(),
                   {'type': 'turn_context', 'payload': {'cwd': str(second_project)}}, reply()]
        self.source.write_bytes(b''.join((json.dumps(r) + '\n').encode() for r in records))
        archive = self.mod.export_session(self.home, self.tid, self.root / 'exports')
        report = self.mod.verify_archive(archive)
        self.assertEqual(len(report['manifest']['attachments']), 2)
        target = self.root / 'target'
        target.mkdir()
        restored = self.mod.restore_session_files(archive, report, target)
        with zipfile.ZipFile(archive) as z:
            changed = self.mod.relink_session(z.read(report['manifest']['record_path']), restored['path_map'])
        result = [json.loads(line) for line in changed.splitlines()]
        for index, source in ((1, first), (3, second)):
            entry = next(f for f in restored['files'] if f['source_path'] == str(source))
            self.assertIn(Path(entry['path']).as_posix(), result[index]['payload']['content'][0]['text'])

    def test_failed_attachment_write_leaves_no_truncated_final_file(self):
        archive = self.export()
        report = self.mod.verify_archive(archive)
        target = self.root / 'target'
        target.mkdir()
        with patch.object(self.mod.session_files.zipfile.ZipFile, 'read', side_effect=OSError('injected read failure')):
            with self.assertRaises(OSError):
                self.mod.session_files.restore_files(archive, report, target)
        asset = report['manifest']['attachments'][0]
        destination = target / 'session-files' / report['sha256'][:20] / asset['id'] / Path(asset['archive_path']).name
        self.assertFalse(destination.exists(), 'An empty or truncated final file must not be preserved as a user edit')
        restored = self.mod.restore_session_files(archive, report, target)
        self.assertEqual(len(restored['files']), 2)
        self.assertEqual(destination.read_bytes(), Path(asset['source_path']).read_bytes())

    def test_old_absolute_paths_in_prose_and_code_blocks_use_restored_files(self):
        old = 'C:/Users/OldUser/project/report.docx'
        target = 'D:/New Project/session-files/report.docx'
        text = '文件在 C:\\Users\\OldUser\\project\\report.docx，稍后修改。\n```\nc:/users/olduser/project/report.docx\n```\n[open](C:/Users/OldUser/project/report.docx)'
        row = {'type': 'response_item', 'payload': {'role': 'assistant', 'content': [{'text': text}]}}
        changed = self.mod.relink_session((json.dumps(row) + '\n').encode(), {old: target})
        result = json.loads(changed)['payload']['content'][0]['text']
        self.assertEqual(result.count(target), 3)
        self.assertNotIn('OldUser', result)
        self.assertIn('[open](<' + target + '>)', result)


if __name__ == '__main__':
    unittest.main()
