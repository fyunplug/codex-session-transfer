import importlib.util
import json
from pathlib import Path
import unittest


class OpaqueReasoningTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('opaque_transfer', Path(__file__).parents[1] / 'scripts/session_transfer.py')
        self.t = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.t)
        self.cipher = 'gAAAA-fake-cipher-ghp_' + 'A' * 64 + '='

    def test_cipher_is_preserved_but_summary_credentials_are_filtered(self):
        for kind in ('reasoning', 'compaction'):
            item = {'type': kind, 'encrypted_content': self.cipher, 'summary': self.cipher}
            clean = self.t.Sanitizer([]).clean(item)
            self.assertEqual(clean['encrypted_content'], self.cipher)
            self.assertNotEqual(clean['summary'], self.cipher)

    def test_json_encoded_reasoning_is_not_scanned_again_as_plaintext(self):
        item = {'type': 'reasoning', 'encrypted_content': self.cipher, 'db_password': 'FAKE_PASSWORD'}
        clean = json.loads(self.t.Sanitizer([]).clean(json.dumps(item)))
        self.assertEqual(clean['encrypted_content'], self.cipher)
        self.assertNotIn('db_password', clean)

    def test_unrecognized_objects_do_not_bypass_credential_filtering(self):
        clean = self.t.Sanitizer([]).clean({'type': 'message', 'encrypted_content': self.cipher})
        self.assertNotEqual(clean['encrypted_content'], self.cipher)

