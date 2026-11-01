import base64
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile


class CredentialPrivacyTests(unittest.TestCase):
    def setUp(self):
        spec=importlib.util.spec_from_file_location('v11_privacy',Path(__file__).parents[1]/'scripts/session_transfer.py')
        self.t=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.t)
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.project=self.root/'project';self.project.mkdir()
        self.password='FAKE_PRIVACY_TEST_VALUE'
        self.uri='postgres://demo:'+self.password+'@example.invalid/db'

    def test_structured_prefixed_and_camel_case_passwords_are_removed(self):
        data={'db_password':self.password,'dbPassword':self.password,'smtp_passwd':self.password,
              'redisPwd':self.password,'auth':base64.b64encode(b'demo:FAKE_VALUE').decode()}
        for value in (data,json.dumps(data),{'nested':json.dumps(data)}):
            cleaned=json.dumps(self.t.Sanitizer([]).clean(value))
            self.assertNotIn(self.password,cleaned)
            self.assertNotIn(base64.b64encode(b'demo:FAKE_VALUE').decode(),cleaned)

    def test_connection_uri_is_redacted_in_chat_and_metadata(self):
        value='DB='+self.uri+' https://demo:FAKE_HTTP_PASSWORD@example.invalid/'
        cleaned=self.t.Sanitizer([]).clean(value)
        self.assertNotIn(self.password,cleaned)
        self.assertNotIn('FAKE_HTTP_PASSWORD',cleaned)
        self.assertFalse(self.t.Sanitizer([]).signature_has_secrets('a.'*100000))

    def test_source_literals_blocked_without_rejecting_expressions(self):
        s=self.t.Sanitizer([])
        for code in ('const dbPassword = "'+self.password+'";', 'const connectionString = "'+self.uri+'";'):
            self.assertTrue(s.source_has_secrets(code))
        for code in ('const dbPassword = process.env.DB_PASSWORD;',
                     'const token = getToken();', 'const connectionString = getConnectionString();',
                     'const url = `postgres://${user}:${password}@host/db`;'):
            self.assertFalse(s.source_has_secrets(code))

    def test_project_excludes_docker_config_passwords_and_uri_but_keeps_code(self):
        (self.project/'.docker').mkdir()
        (self.project/'.docker/config.json').write_text(json.dumps({'auths':{'example.invalid':{'auth':'ZGVtbzpGQUtF'}}}))
        (self.project/'settings.json').write_text(json.dumps({'db_password':self.password}))
        (self.project/'connection.js').write_text('const connectionString = "'+self.uri+'";')
        (self.project/'helper.js').write_text('const token = getToken();')
        p=self.root/'project.zip'
        self.t.project_transfer.export_project(self.project,p,self.t.Sanitizer([]),self.t.session_files)
        with zipfile.ZipFile(p) as z:
            self.assertIn('project/helper.js',z.namelist())
            self.assertNotIn('project/.docker/config.json',z.namelist())
            self.assertNotIn('project/settings.json',z.namelist())
            self.assertNotIn('project/connection.js',z.namelist())

    def test_attachments_and_nested_zip_use_the_same_policy(self):
        p=self.project/'settings.json';p.write_text(json.dumps({'db_password':self.password}))
        rows=[{'type':'response_item','payload':{'role':'assistant','content':[{'text':f'[config]({p.as_posix()})'}]}}]
        assets,report,_=self.t.session_files.collect_files(rows,str(self.project),self.root/'home',self.t.Sanitizer([]),self.t.LIMIT)
        self.assertEqual(assets,[]);self.assertEqual(report[0]['reason'],'possible_credentials')
        for name,content in (('settings.json',p.read_bytes()),('src/connection.js',('const c="'+self.uri+'";').encode()),
                             ('.docker/config.json',b'{}')):
            buffer=io.BytesIO()
            with zipfile.ZipFile(buffer,'w') as z:z.writestr(name,content)
            self.assertTrue(self.t.session_files.contains_secrets(buffer.getvalue(),self.t.Sanitizer([])))

    def test_binary_embedded_connection_uri_is_not_exported(self):
        (self.project/'program.exe').write_bytes(b'MZ\0'+self.uri.encode()+b'\0')
        p=self.root/'binary.zip'
        self.t.project_transfer.export_project(self.project,p,self.t.Sanitizer([]),self.t.session_files)
        with zipfile.ZipFile(p) as z:self.assertNotIn('project/program.exe',z.namelist())

    def test_custom_codex_home_inside_project_is_never_collected(self):
        from unittest.mock import patch
        home=self.project/'custom-state';(home/'sessions').mkdir(parents=True)
        (home/'sessions/other.jsonl').write_text('PRIVATE_OTHER_CONVERSATION')
        (self.project/'main.py').write_text('print(1)')
        p=self.root/'private-home.zip'
        with patch.dict('os.environ',{'CODEX_HOME':str(home)}):
            self.t.project_transfer.export_project(self.project,p,self.t.Sanitizer([]),self.t.session_files)
        with zipfile.ZipFile(p) as z:
            self.assertIn('project/main.py',z.namelist())
            self.assertFalse(any(n.startswith('project/custom-state/') for n in z.namelist()))

    def test_explicit_home_and_separate_sqlite_root_are_private(self):
        home=self.project/'selected-state';home.mkdir()
        sqlite=self.project/'database-state';sqlite.mkdir()
        (home/'config.toml').write_text('sqlite_home = '+repr(str(sqlite)),encoding='utf-8')
        (sqlite/'unrelated-history.jsonl').write_text('PRIVATE_OTHER_CONVERSATION')
        p=self.root/'selected-home.zip'
        self.t.project_transfer.export_project(self.project,p,self.t.Sanitizer([]),self.t.session_files,home=home)
        with zipfile.ZipFile(p) as z:
            self.assertFalse(any('database-state/' in n or 'selected-state/' in n for n in z.namelist()))
