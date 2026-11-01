import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


class ExportTargetTests(unittest.TestCase):
    def load(self):
        spec = importlib.util.spec_from_file_location('target_test', Path(__file__).parents[1] / 'scripts/session_transfer.py')
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_explicit_chat_reference_is_detected_without_treating_it_as_history(self):
        mod = self.load()
        self.assertTrue(hasattr(mod, 'referenced_export_targets'))
        target = '019f9e4c-5597-7482-a634-d287dadec327'
        rows = [{'type': 'response_item', 'payload': {'role': 'user', 'content': [{'text':
            '## Referenced chats with Codex:\n[]\n## My request:\n[old](thread://' + target + '?hostId=local) $codex-session-transfer 导出当前会话'}]}},
            {'type': 'response_item', 'payload': {'role': 'user', 'content': [{'text': '<skill>injected instructions</skill>'}]}}]
        self.assertEqual(mod.referenced_export_targets(rows), [target])
        self.assertEqual(mod.referenced_export_targets(rows + [{'type': 'response_item', 'payload': {'role': 'user', 'content': [{'text': 'Export this current conversation instead'}]}}]), [])

    def test_unreadable_optional_index_does_not_block_session_export(self):
        mod = self.load()
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as tmp:
            root = Path(tmp)
            home = root / 'home'
            (home / 'sessions').mkdir(parents=True)
            tid = '11111111-1111-4111-8111-111111111111'
            (home / 'sessions/test.jsonl').write_text(json.dumps({'type': 'session_meta', 'payload': {'id': tid}}) + '\n')
            (home / 'state_9.sqlite').write_bytes(b'not a valid optional database')
            path = mod.export_session(home, tid, root / 'exports')
            report = mod.verify_archive(path)
            self.assertEqual(report['thread_id'], tid)
            self.assertTrue(any(r.get('status') == 'unavailable_optional_index' for r in report['manifest']['metadata_sources']))
