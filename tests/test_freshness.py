import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unanimis.core import Core
from unanimis import librarian
from unanimis.cli import main


class FreshnessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.core = Core(self.temp.name)
        self.source = self.core.store('The routing blocker is unresolved.', 'source', title='Routing checkpoint')
        self.rid = self.source['record_id']
        self.report = self.core.store('The routing blocker is unresolved according to the old checkpoint.', 'report', title='Routing report', kind='review', metadata=dict(status='proposed', librarian_version='librarian-v1', target=dict(record_id=self.rid, revision=1), analysis=dict(summary='Original summary', evidence=[dict(record_id=self.rid, revision=1, quote='The routing blocker is unresolved.')])) )
        self.report_id = self.report['record_id']

    def tearDown(self):
        self.core.close(); self.temp.cleanup()

    def change_source(self):
        self.core.correct(self.rid, 1, 'The routing blocker was fixed in a later test.', 'Observed fix', 'fix-source')

    def test_freshness_is_separate_from_revision_state_and_reaches_recall(self):
        self.assertEqual(self.core.get(self.rid)['freshness']['status'], 'not_assessed')
        self.assertEqual(self.core.get(self.report_id)['freshness']['status'], 'references_current')
        self.change_source()
        report = self.core.get(self.report_id)
        self.assertEqual(report['state'], 'current')
        self.assertEqual(report['freshness']['status'], 'stale')
        self.assertEqual(report['freshness']['references'][0]['cited_revision'], 1)
        self.assertEqual(report['freshness']['references'][0]['current_revision'], 2)
        match = next(r for r in self.core.recall('routing')['matches'] if r['record_id'] == self.report_id)
        self.assertEqual(match['freshness']['status'], 'stale')
        self.assertEqual(librarian.report_status(self.core, report)['changed_sources'], [self.rid])

    def test_cli_warns_before_body_and_json_and_read_only_mcp_expose_same_status(self):
        self.change_source()
        for args in (['get', self.report_id], ['recall', 'routing']):
            out = io.StringIO()
            with contextlib.redirect_stdout(out): main(['--data-dir', self.temp.name] + args)
            text = out.getvalue()
            self.assertIn('STALE EVIDENCE', text)
            self.assertIn('cited revision 1; current revision 2', text)
            self.assertLess(text.index('STALE EVIDENCE'), text.index('unresolved according to the old checkpoint'))
        out = io.StringIO()
        with contextlib.redirect_stdout(out): main(['--data-dir', self.temp.name, '--json', 'get', self.report_id])
        self.assertEqual(json.loads(out.getvalue())['freshness']['status'], 'stale')
        messages = [dict(jsonrpc='2.0', id=1, method='tools/list', params={}), dict(jsonrpc='2.0', id=2, method='tools/call', params=dict(name='unim_get', arguments=dict(record_id=self.report_id))), dict(jsonrpc='2.0', id=3, method='tools/call', params=dict(name='unim_recall', arguments=dict(query='routing')))]
        messages = [dict(jsonrpc='2.0', id=0, method='initialize', params=dict(protocolVersion='2024-11-05', capabilities={}, clientInfo=dict(name='freshness-test', version='1'))), dict(jsonrpc='2.0', method='notifications/initialized')] + messages
        process = subprocess.run([sys.executable, '-m', 'unanimis', '--data-dir', self.temp.name, 'mcp', '--read-only'], input='\n'.join(json.dumps(x) for x in messages)+'\n', text=True, capture_output=True, timeout=15)
        self.assertEqual(process.returncode, 0, process.stderr)
        results = {x['id']:x['result'] for x in map(json.loads, process.stdout.splitlines())}
        self.assertEqual({t['name'] for t in results[1]['tools']}, {'unim_get', 'unim_recall'})
        retrieved = json.loads(results[2]['content'][0]['text'])
        self.assertEqual(retrieved['freshness']['status'], 'stale')
        recalled = json.loads(results[3]['content'][0]['text'])
        self.assertEqual(next(x for x in recalled['matches'] if x['record_id']==self.report_id)['freshness']['status'], 'stale')

    def test_accepted_and_historical_reports_keep_freshness_without_rewriting(self):
        original = self.core.get(self.report_id)
        proposal = self.core.propose(record_id=self.report_id, expected_revision=1, title=original['title'], content='Reviewed routing summary.', summary='Clarify source attribution', tags=[], evidence=[dict(record_id=self.rid, revision=1, quote='The routing blocker is unresolved.'), dict(record_id=self.report_id, revision=1, quote=original['content'])], request_id='review-report')
        self.core.approve(proposal['proposal_id'])
        self.assertEqual(self.core.get(self.report_id)['freshness']['status'], 'references_current')
        self.change_source()
        report = self.core.get(self.report_id)
        self.assertEqual(report['acceptance'], 'accepted editorial revision')
        self.assertEqual(report['freshness']['status'], 'stale')
        old = self.core.get(self.report_id, 1)
        self.assertEqual(old['state'], 'superseded')
        self.assertEqual(old['freshness']['status'], 'stale')
        self.assertEqual(old['content'], original['content'])
        self.assertEqual(old['provenance'], original['provenance'])
        self.assertEqual(self.core.get(self.report_id)['revision'], 2)

    def test_unavailable_and_cross_scope_references_do_not_leak_current_revisions(self):
        other = Core(self.temp.name, scope='private')
        hidden = other.store('Private source', 'private')
        other.close()
        for rid, revision in [(hidden['record_id'], 1), ('missing', 1), (self.rid, 99)]:
            r = self.core.store('A report with unavailable evidence.', 'ref-'+rid, kind='review', metadata=dict(librarian_version='librarian-v1', target=dict(record_id=rid, revision=revision), analysis=dict(evidence=[dict(record_id=rid, revision=revision, quote='test')])) )
            freshness = self.core.get(r['record_id'])['freshness']
            self.assertEqual(freshness['status'], 'unavailable')
            if rid != self.rid: self.assertIsNone(freshness['references'][0]['current_revision'])
        malformed = self.core.store('Malformed metadata is untrusted data.', 'malformed', metadata=dict(librarian_version='librarian-v1', analysis=None))
        self.assertEqual(self.core.get(malformed['record_id'])['freshness']['status'], 'unavailable')

    def test_checks_are_bounded_and_do_not_recurse_into_citation_cycles(self):
        # Capture before any source exists, then add another report citing it.
        first = self.core.store('A', 'cycle-a', kind='review')
        second = self.core.store('B', 'cycle-b', kind='review', metadata=dict(librarian_version='librarian-v1', target=dict(record_id=first['record_id'], revision=1), analysis=dict(evidence=[])))
        p = self.core.propose(record_id=first['record_id'], expected_revision=1, title='A', content='A cites B', summary='Reference B', tags=[], evidence=[dict(record_id=second['record_id'], revision=1, quote='B')], request_id='cycle')
        self.core.approve(p['proposal_id'])
        self.assertEqual(self.core.get(first['record_id'])['freshness']['status'], 'references_current')
        self.assertEqual(self.core.get(second['record_id'])['freshness']['status'], 'stale')
        # Result explicitly says direct checks, so this is not a claim about downstream freshness.
        self.assertEqual(self.core.get(first['record_id'])['freshness']['basis'], 'direct_record_revision_references')
        many = self.core.store('Many references', 'many', kind='review', metadata=dict(librarian_version='librarian-v1', target=dict(record_id=self.rid, revision=1), analysis=dict(evidence=[dict(record_id='missing-'+str(i), revision=1, quote='x') for i in range(70)])))
        freshness = self.core.get(many['record_id'])['freshness']
        self.assertTrue(freshness['checks_truncated'])
        self.assertEqual(len(freshness['references']), 64)
        self.assertEqual(freshness['status'], 'unavailable')
