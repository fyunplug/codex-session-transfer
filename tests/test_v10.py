import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import zipfile


class V10Tests(unittest.TestCase):
    def setUp(self):
        spec=importlib.util.spec_from_file_location('v10',Path(__file__).parents[1]/'scripts/session_transfer.py')
        self.t=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.t)
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.project=self.root/'project';self.project.mkdir()

    def build(self):
        return self.t.project_transfer.export_project(self.project,self.root/'project.zip',self.t.Sanitizer([]),self.t.session_files)

    def test_code_expressions_are_preserved_but_literal_secrets_are_filtered(self):
        (self.project/'package.json').write_text(json.dumps({'main':'index.js'}))
        (self.project/'index.js').write_text("require('./helper')")
        (self.project/'helper.js').write_text('const token = getToken(); const password = process.env.PWD;')
        (self.project/'literal.js').write_text('const password = "PRIVATE_TEST_LITERAL";')
        (self.project/'key.js').write_text('const value = "sk-proj-'+'A'*40+'";')
        (self.project/'pin.py').write_text('password = 123456')
        r=self.build()
        with zipfile.ZipFile(self.root/'project.zip') as z:
            self.assertIn('project/helper.js',z.namelist())
            self.assertNotIn('project/literal.js',z.namelist())
            self.assertNotIn('project/key.js',z.namelist())
            self.assertNotIn('project/pin.py',z.namelist())
        self.assertEqual(r['critical_checks']['status'],'passed')

    def test_linked_code_attachment_uses_the_same_scan(self):
        p=self.project/'helper.py';p.write_text('token = get_token()\npassword = os.getenv("PWD")')
        rows=[{'type':'response_item','payload':{'role':'assistant','content':[{'text':f'[code]({p.as_posix()})'}]}}]
        assets,skipped,_=self.t.session_files.collect_files(rows,str(self.project),self.root/'home',self.t.Sanitizer([]),self.t.LIMIT)
        self.assertEqual(len(assets),1);self.assertEqual(skipped,[])

    def test_root_electron_missing_entry_is_rejected(self):
        (self.project/'resources/app').mkdir(parents=True);(self.project/'locales').mkdir()
        (self.project/'resources/app/package.json').write_text(json.dumps({'main':'missing.cjs'}))
        for n in ('program.exe','ffmpeg.dll','icudtl.dat','resources.pak','locales/en.pak'):
            (self.project/n).write_bytes(b'MZ_STUB')
        with self.assertRaisesRegex(ValueError,'missing.cjs'):self.build()

    def test_reports_share_one_inventory(self):
        home=self.root/'home';(home/'sessions').mkdir(parents=True)
        p=self.project/'program.exe';p.write_bytes(b'MZ_RUNTIME')
        tid='12345678-1234-4234-8234-123456789abc'
        rows=[{'type':'session_meta','payload':{'id':tid,'cwd':str(self.project)}},
              {'type':'response_item','payload':{'role':'assistant','content':[{'text':f'[app]({p.as_posix()})'}]}}]
        (home/'sessions/a.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows))
        from unittest.mock import patch
        with patch.object(self.t.session_files,'FILE_LIMIT',4):
            archive=self.t.export_session(home,tid,self.root/'exports',project_root=self.project)
        with zipfile.ZipFile(self.t.resolve_archive(archive)) as z:
            m=json.loads(z.read('manifest.json'));c=json.loads(z.read('COMPLETENESS-REPORT.json'))
            inventory=m['file_inventory']
            self.assertEqual(c['file_inventory'],inventory)
            self.assertEqual(len(inventory['project_covered']),1)
            self.assertEqual(inventory['missing'],[])
            text=z.read('FILE-REPORT.txt').decode('utf-8')
            self.assertIn('项目已覆盖',text)
            self.assertNotIn('Not included (reason)',text)
        classified=self.t.session_files.file_inventory([{'source_path':'a'}], [
            {'reference':'b','reason':'size_or_count_limit','covered_by_project':True},
            {'reference':'c','reason':'sensitive_path'}, {'reference':'d','reason':'not_found'}])
        for key in ('attachment_included','project_covered','excluded','missing'):
            self.assertEqual(len(classified[key]),1)
