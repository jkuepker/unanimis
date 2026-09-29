import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unanimis.core import Core, UnimError
from unanimis.work import Board, boards, server

SCHEMA = '''
CREATE TABLE tasks(id TEXT PRIMARY KEY, title TEXT, body TEXT, status TEXT, assignee TEXT,
priority INTEGER, created_at INTEGER, started_at INTEGER, completed_at INTEGER,
current_run_id INTEGER, claim_lock TEXT, claim_expires INTEGER, last_heartbeat_at INTEGER,
block_kind TEXT, last_failure_error TEXT, result TEXT, workspace_path TEXT, branch_name TEXT, completion_contract TEXT);
CREATE TABLE task_runs(id INTEGER PRIMARY KEY, task_id TEXT, profile TEXT, status TEXT,
claim_expires INTEGER, last_heartbeat_at INTEGER, started_at INTEGER, ended_at INTEGER,
outcome TEXT, summary TEXT, error TEXT);
CREATE TABLE task_comments(id INTEGER PRIMARY KEY, task_id TEXT, author TEXT, body TEXT, created_at INTEGER);
CREATE TABLE task_links(parent_id TEXT, child_id TEXT);
'''

class WorkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / 'kanban/boards/fixture/kanban.db'
        self.path.parent.mkdir(parents=True)
        self.db = sqlite3.connect(self.path)
        self.db.executescript(SCHEMA)
        self.db.execute("INSERT INTO tasks(id,title,status,assignee,created_at,current_run_id,claim_lock,claim_expires,result) VALUES('one','Fixture','running','worker',1,1,'secret-claim-token',1,'reported evidence')")
        self.db.execute("INSERT INTO task_runs(id,task_id,profile,status) VALUES(1,'one','worker','running')")
        self.db.commit()
        self.core = Core(self.root / 'memory')
        self.view = Board(self.root, 'fixture', self.core)

    def tearDown(self):
        self.core.close(); self.db.close(); self.temp.cleanup()

    def test_read_state_is_fresh_paginated_and_cannot_write_even_after_pragma(self):
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute("INSERT INTO tasks(id,title,status,created_at) VALUES('two','Parent','blocked',2)")
        self.db.execute("INSERT INTO task_links VALUES('two','one')")
        for i in range(3):
            self.db.execute('INSERT INTO task_comments(task_id,author,body,created_at) VALUES(?,?,?,?)', ('one','tester',str(i),i))
        self.db.commit()
        before = list(self.db.iterdump())
        page = self.view.work_list(limit=1)
        self.assertEqual(page['tasks'][0]['id'], 'two')
        self.assertEqual(page['next_offset'], 1)
        task = self.view.work_get('one',limit=1)
        self.assertEqual(task['task']['claim_state'], 'expired')
        self.assertNotIn('secret-claim-token', json.dumps(task))
        self.assertEqual(task['dependencies'][0]['status'], 'blocked')
        self.assertEqual(task['comments_next_offset'], 1)
        self.assertEqual(self.view.work_get('one',limit=1,offset=1)['comments'][0]['body'], '1')
        with self.view.read() as ro:
            ro.execute('PRAGMA query_only=OFF')
            with self.assertRaises(sqlite3.OperationalError): ro.execute('DELETE FROM tasks')
        self.assertEqual(list(self.db.iterdump()), before)
        self.db.execute("UPDATE tasks SET status='done' WHERE id='one'"); self.db.commit()
        self.assertEqual(self.view.work_get('one')['task']['completion'], 'reported_done')
        self.assertEqual(self.view.work_get('one')['task']['worker_liveness'], 'not checked')

    def test_checkpoint_is_idempotent_run_bound_and_scope_filtered(self):
        before = list(self.db.iterdump())
        capture = self.view.checkpoint('one','Exact handoff\nEvidence: fixture','codex','same',1)
        self.assertEqual(self.core.get(capture['record_id'])['content'], 'Exact handoff\nEvidence: fixture')
        self.assertEqual(self.view.work_get('one')['checkpoints'][0]['record_id'], capture['record_id'])
        self.db.execute("UPDATE tasks SET current_run_id=2 WHERE id='one'"); self.db.commit()
        self.assertEqual(self.view.checkpoint('one','Exact handoff\nEvidence: fixture','codex','same',1)['record_id'], capture['record_id'])
        with self.assertRaisesRegex(UnimError, 'different input'): self.view.checkpoint('one','different','codex','same',1)
        with self.assertRaisesRegex(UnimError, 'run changed'): self.view.checkpoint('one','late handoff','codex','new',1)
        other = Core(self.root/'memory',project='other')
        try: self.assertEqual(Board(self.root,'fixture',other).work_get('one')['checkpoints'], [])
        finally: other.close()
        self.db.execute("UPDATE tasks SET current_run_id=1 WHERE id='one'"); self.db.commit()
        self.assertEqual(list(self.db.iterdump()), before)
        self.assertEqual(len(self.core.list_records()),1)

    def test_explicit_board_missing_schema_and_path_escape_fail_closed(self):
        self.assertEqual(boards(self.root)['boards'][0]['board'], 'fixture')
        for invalid in ('../fixture','',None,'/tmp'):
            with self.assertRaises(UnimError): Board(self.root,invalid)
        with self.assertRaisesRegex(UnimError,'Cannot read'): Board(self.root,'missing').work_list()
        self.assertFalse((self.root/'kanban/boards/missing').exists())
        self.db.execute('DROP TABLE task_runs'); self.db.commit()
        with self.assertRaisesRegex(UnimError,'Unsupported Hermes schema'): self.view.work_list()

    def test_separate_mcp_exposes_reads_only_and_live_mutations_are_reflected(self):
        ro = Core(self.root/'memory',read_only=True)
        try:
            s = server(ro,self.root,'fixture')
            def call(method, params=None): return s.dispatch(dict(jsonrpc='2.0',id=1,method=method,params=params or {}))
            call('initialize',dict(protocolVersion='2025-11-25',capabilities={},clientInfo={'name':'fixture','version':'1'}))
            s.dispatch(dict(jsonrpc='2.0',method='notifications/initialized'))
            definitions = call('tools/list')['result']['tools']
            self.assertEqual({t['name'] for t in definitions},{'unim_work_list','unim_work_get'})
            self.assertTrue(all(t['annotations']['readOnlyHint'] for t in definitions))
            for name in ('unim_store','unim_capture_new','unim_work_claim','unim_work_complete'):
                self.assertIn('error',call('tools/call',{'name':name,'arguments':{}}))
            self.assertTrue(call('tools/call',{'name':'unim_work_list','arguments':{'project':'other'}})['result']['isError'])
            self.db.execute("UPDATE tasks SET status='blocked' WHERE id='one'"); self.db.commit()
            answer=call('tools/call',{'name':'unim_work_get','arguments':{'task_id':'one'}})
            self.assertEqual(answer['result']['structuredContent']['task']['status'],'blocked')
            self.assertIn('error',call('tools/call',{'name':'unim_work_get','arguments':{'task_id':'one','board':'other'}}))
        finally: ro.close()
        args=[sys.executable,'-m', 'unanimis','--data-dir',str(self.root/'memory'),'--json','work','--hermes-root',str(self.root)]
        p=subprocess.run(args+['--board','fixture','show','one'],capture_output=True,text=True)
        self.assertEqual(p.returncode,0,p.stderr)
        self.assertEqual(json.loads(p.stdout)['task']['status'],'blocked')
        p=subprocess.run(args+['list'],capture_output=True,text=True)
        self.assertNotEqual(p.returncode,0)
        self.assertIn('Select --board',p.stderr)
