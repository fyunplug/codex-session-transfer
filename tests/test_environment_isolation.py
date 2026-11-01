import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import zipfile


class EnvironmentIsolationTests(unittest.TestCase):
    def setUp(self):
        spec=importlib.util.spec_from_file_location('isolation_test',Path(__file__).parents[1]/'scripts/native_import.py')
        self.native=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.native)
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.project=self.root/'project';self.project.mkdir()
        self.home=self.root/'codex-home';self.home.mkdir()
        self.sqlite=self.project/'sqlite-state';self.sqlite.mkdir()

    def test_environment_sqlite_home_is_excluded_from_project(self):
        (self.sqlite/'unrelated.jsonl').write_text('PRIVATE_OTHER_CONVERSATION')
        (self.project/'main.py').write_text('print(1)')
        archive=self.root/'project.zip'
        with patch.dict(os.environ,{'CODEX_SQLITE_HOME':str(self.sqlite)}):
            self.native.transfer.project_transfer.export_project(self.project,archive,self.native.transfer.Sanitizer([]),self.native.transfer.session_files,home=self.home)
        with zipfile.ZipFile(archive) as z:
            self.assertIn('project/main.py',z.namelist())
            self.assertFalse(any('sqlite-state/' in n for n in z.namelist()))

    def test_isolated_server_does_not_inherit_live_sqlite_home(self):
        from unittest.mock import MagicMock
        isolated=self.root/'isolated';isolated.mkdir()
        proc=MagicMock();proc.stdout=io.StringIO('');proc.stdin=io.StringIO()
        with patch.dict(os.environ,{'CODEX_SQLITE_HOME':str(self.sqlite)}), \
             patch.object(self.native.subprocess,'Popen',return_value=proc) as launch, \
             patch.object(self.native.AppServer,'call',return_value={}), \
             patch.object(self.native.AppServer,'send'):
            server=self.native.AppServer(self.root/'codex.exe',isolated,isolated=True)
            self.assertEqual(launch.call_args.kwargs['env']['CODEX_SQLITE_HOME'],str(isolated))
            server.close()

    def test_environment_sqlite_index_is_backed_up_and_selected(self):
        db=self.sqlite/'state_9.sqlite';con=sqlite3.connect(db)
        con.execute('CREATE TABLE threads (id TEXT, title TEXT)')
        con.execute('INSERT INTO threads VALUES (?,?)',('fake-thread','example'))
        con.commit();con.close()
        with patch.dict(os.environ,{'CODEX_HOME':str(self.home),'CODEX_SQLITE_HOME':str(self.sqlite)}):
            self.native.backup_indexes(self.home,self.root/'backup')
            values,inventory=self.native.transfer.metadata(self.home,'fake-thread')
        self.assertEqual(len(list((self.root/'backup').glob('*-state_9.sqlite'))),1)
        self.assertTrue(self.native.backup_complete(self.root/'backup'))
        self.assertEqual(values['state_9-thread'][0]['id'],'fake-thread')

    def test_alternate_home_does_not_inherit_an_unrelated_live_sqlite(self):
        db=self.sqlite/'state_9.sqlite';con=sqlite3.connect(db)
        con.execute('CREATE TABLE threads (id TEXT, title TEXT)');con.commit();con.close()
        active=self.root/'active-home';active.mkdir()
        with patch.dict(os.environ,{'CODEX_HOME':str(active),'CODEX_SQLITE_HOME':str(self.sqlite)}):
            self.assertFalse(self.native.transfer.use_environment_sqlite(self.home))
            self.native.backup_indexes(self.home,self.root/'alternate-backup')
        self.assertFalse(list((self.root/'alternate-backup').glob('*-state_9.sqlite')))
