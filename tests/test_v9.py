import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import zipfile


class V9Tests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('v9', Path(__file__).parents[1] / 'scripts/session_transfer.py')
        self.t = importlib.util.module_from_spec(spec); spec.loader.exec_module(self.t)
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.project = self.root / 'project'; self.project.mkdir()
        self.home = self.root / 'home'; (self.home / 'sessions').mkdir(parents=True)
        self.tid = '12345678-1234-4234-8234-123456789abc'
        self.rows = [{'type':'session_meta','payload':{'id':self.tid,'cwd':str(self.project)}}]

    def export(self, **kwargs):
        (self.home / 'sessions/source.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in self.rows))
        return self.t.export_session(self.home,self.tid,self.project/'session-exports',**kwargs)

    def test_credentials_and_runtime_dependencies(self):
        for name in ('.netrc','_netrc','.git-credentials'):
            (self.project/name).write_text('machine example.invalid login demo password FAKE_VALUE')
        dep = self.project/'software/resources/app/node_modules/demo'; dep.mkdir(parents=True)
        (dep/'index.js').write_text('module.exports=1')
        (dep/'.netrc').write_text('password FAKE_VALUE')
        p=self.t.project_transfer.export_project(self.project,self.root/'project.zip',self.t.Sanitizer([]),self.t.session_files)
        with zipfile.ZipFile(self.root/'project.zip') as z:
            self.assertIn('project/software/resources/app/node_modules/demo/index.js',z.namelist())
            self.assertFalse(any(n.endswith(('.netrc','_netrc','.git-credentials')) for n in z.namelist()))

    def test_default_project_output_and_completeness(self):
        (self.project/'main.py').write_text('print(1)')
        p=self.export(project_root=self.project)
        self.assertFalse(p.is_relative_to(self.project))
        report=self.t.verify_archive(p)['manifest']['completeness']
        self.assertEqual(report['history']['status'],'verified')
        self.assertIn('runtime',report)

    def test_missing_entry_stops_project_export(self):
        (self.project/'package.json').write_text(json.dumps({'main':'missing.js'}))
        with self.assertRaisesRegex(ValueError,'Critical project files'):
            self.t.project_transfer.export_project(self.project,self.root/'bad.zip',self.t.Sanitizer([]),self.t.session_files)
        self.assertFalse((self.root/'bad.zip').exists())

    def test_latest_uses_last_reference_not_highest_version_or_file_mtime(self):
        paths=[self.project/n for n in ('report-v1.txt','report-v2.txt','different.txt')]
        for p in paths:p.write_text(p.stem)
        for p in [paths[0],paths[1],paths[2],paths[0]]:
            self.rows.append({'type':'response_item','payload':{'role':'assistant','content':[{'text':f'[file]({p.as_posix()})'}]}})
        all_pack=self.export()
        self.assertEqual(len(self.t.verify_archive(all_pack)['manifest']['attachments']),3)
        latest=self.export(attachment_mode='latest')
        m=self.t.verify_archive(latest)['manifest']
        self.assertEqual({Path(a['source_path']).name for a in m['attachments']},{'report-v1.txt','different.txt'})
        self.assertTrue(any(r['reason']=='superseded_by_later_reference' for r in m['file_report']))

    def test_bad_single_manifest_has_clear_error(self):
        p=self.root/'bad.zip'
        with zipfile.ZipFile(p,'w') as z:
            z.writestr('bundle-manifest.json',json.dumps({'format':'codex-single-transfer-v1','session':{},'files':{'a.zip':{},'b.zip':{}}}))
            z.writestr('a.zip',b'a');z.writestr('b.zip',b'b')
        with self.assertRaisesRegex(ValueError,'manifest'):
            self.t.resolve_archive(p)

    def test_missing_latest_does_not_silently_fall_back_to_old(self):
        for name in ('report-v1.txt','report-v2.txt'):
            p=self.project/name
            if 'v1' in name:p.write_text('old')
            self.rows.append({'type':'response_item','payload':{'role':'assistant','content':[{'text':f'[file]({p.as_posix()})'}]}})
        m=self.t.verify_archive(self.export(attachment_mode='latest'))['manifest']
        self.assertEqual(m['attachments'],[])
        self.assertTrue(any(r['reason']=='not_found' for r in m['file_report']))

    def test_missing_electron_resources_is_not_claimed_runnable(self):
        base=self.project/'software';(base/'resources/app').mkdir(parents=True)
        (base/'program.exe').write_bytes(b'MZ')
        (base/'resources/app/package.json').write_text('{}')
        with self.assertRaisesRegex(ValueError,'Critical project files'):
            self.t.project_transfer.export_project(self.project,self.root/'bad.zip',self.t.Sanitizer([]),self.t.session_files)

    def test_missing_project_manifest_has_clear_error(self):
        p=self.root/'bad.zip'
        with zipfile.ZipFile(p,'w') as z:z.writestr('project/main.js','console.log(1)')
        with self.assertRaisesRegex(ValueError,'project manifest'):
            self.t.project_transfer.verify_project(p,None,self.t.session_files)

    def test_historical_snapshot_is_not_an_active_application(self):
        snapshot=self.project/'inspection/old';snapshot.mkdir(parents=True)
        (snapshot/'package.json').write_text(json.dumps({'main':'missing-old-entry.js'}))
        (self.project/'package.json').write_text(json.dumps({'main':'main.js'}))
        (self.project/'main.js').write_text('console.log(1)')
        result=self.t.project_transfer.export_project(self.project,self.root/'ok.zip',self.t.Sanitizer([]),self.t.session_files)
        self.assertEqual(result['critical_checks']['status'],'passed')
