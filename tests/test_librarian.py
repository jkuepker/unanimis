import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from unanimis.core import Core, UnimError
from unanimis import librarian as lib
from unanimis.cli import main


class LibrarianTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.core = Core(self.temp.name)
        lib.configure(self.core, 'http://localhost:8000/v1', 'fixture')
        self.target = self.core.store('I learned that Spanish casa means house. Practice tomorrow.', 'source', title='Spanish learning')
        self.rid = self.target['record_id']
        self.calls = 0

    def tearDown(self):
        self.core.close(); self.temp.cleanup()

    def model(self, cfg, prompt):
        self.calls += 1
        r = prompt['target']
        return dict(summary='The writer learned the Spanish word casa and plans practice.', category='learning', tags=['spanish'], connections=[], cleanup=['Add an example sentence when available.'], evidence=[dict(record_id=r['record_id'], revision=r['revision'], quote='casa means house')]), {'prompt_tokens': 200, 'completion_tokens': 100}

    def test_reports_preserve_source_skip_unchanged_and_refresh_changed(self):
        first = lib.run(self.core, record_id=self.rid, model_call=self.model)
        self.assertEqual(len(first['saved']), 1)
        self.assertEqual(self.core.get(self.rid)['revision'], 1)
        self.assertEqual(lib.run(self.core, model_call=self.model)['saved'], [])
        self.assertEqual(self.calls, 1)
        self.core.correct(self.rid, 1, 'Spanish casa means house. Practiced today.', 'human update', 'correction')
        self.assertTrue(lib.reports(self.core)['reports'][0]['stale'])
        next_run = lib.run(self.core, model_call=self.model)
        self.assertEqual(len(next_run['saved']), 1)
        self.assertEqual(len(lib.reports(self.core)['reports']), 2)

    def test_invalid_quotes_unknown_sources_and_changed_during_call_save_nothing(self):
        for mutation in ('quote', 'record_id', 'revision'):
            def bad(cfg, prompt):
                a, usage = self.model(cfg, prompt)
                a['evidence'][0][mutation] = {'quote': 'invented quote', 'record_id': 'unknown', 'revision': True}[mutation]
                return a, usage
            result = lib.run(self.core, model_call=bad)
            self.assertEqual(result['saved'], [])
            self.assertEqual(result['errors'][0]['code'], 'invalid_evidence')
        def racing(cfg, prompt):
            a, u = self.model(cfg, prompt)
            self.core.correct(self.rid, 1, 'New source', 'human update', 'race')
            return a, u
        self.assertEqual(lib.run(self.core, model_call=racing)['errors'][0]['code'], 'conflict')
        self.assertEqual(lib.reports(self.core)['reports'], [])

    def test_batch_limits_exclude_derived_archived_and_cross_scope(self):
        self.core.store('Source two', 'two')
        self.core.store('Archived', 'archive', kind='archive')
        self.core.store('Review', 'review', kind='review')
        other = Core(self.temp.name, scope='elsewhere')
        other.store('Other scope', 'other'); other.close()
        result = lib.run(self.core, limit=1, model_call=self.model)
        self.assertEqual(len(result['saved']), 1)
        self.assertEqual(result['remaining'], 1)
        self.assertEqual(self.calls, 1)
        readonly = Core(self.temp.name, read_only=True)
        try:
            with self.assertRaisesRegex(UnimError, 'write access'):
                lib.run(readonly, model_call=self.model)
        finally: readonly.close()

    def test_draft_is_pending_and_approval_rejects_changed_evidence(self):
        report = lib.run(self.core, record_id=self.rid, model_call=self.model)['saved'][0]
        def draft_model(cfg, prompt):
            return dict(title='Spanish learning', content=self.core.get(self.rid)['content'] + '\nExample: una casa.', summary='Add a simple example.', tags=['spanish'], evidence=prompt['analysis']['evidence']), {}
        proposal = lib.draft(self.core, report['record_id'], model_call=draft_model)
        self.assertEqual(proposal['status'], 'pending')
        self.assertEqual(self.core.get(self.rid)['revision'], 1)
        self.assertTrue(lib.draft(self.core, report['record_id'], model_call=draft_model)['reused'])
        self.core.correct(self.rid, 1, 'Updated note', 'human update', 'updated')
        with self.assertRaises(UnimError): self.core.approve(proposal['proposal_id'])
        with self.assertRaises(UnimError): lib.draft(self.core, report['record_id'], model_call=draft_model)

    def test_large_sources_and_incomplete_model_output_fail_closed(self):
        large = self.core.store('x' * 24001, 'large')
        result = lib.run(self.core, record_id=large['record_id'], model_call=self.model)
        self.assertEqual(result['errors'][0]['code'], 'too_large')
        self.assertEqual(self.calls, 0)
        with patch('unanimis.librarian.urllib.request.build_opener') as opener:
            response = opener.return_value.open.return_value.__enter__.return_value
            response.read.return_value = json.dumps({'choices': [{'finish_reason': 'length', 'message': {'content': '{}'}}]}).encode()
            with self.assertRaisesRegex(UnimError, 'finish normally'): lib.complete(lib.settings(self.core), {})
        with self.assertRaisesRegex(UnimError, 'redirected'):
            lib.NoRedirect().redirect_request(None, None, 302, '', {}, 'http://example.com')

    def test_connected_records_need_evidence_and_staleness_tracks_them(self):
        related = self.core.store('Spanish casa: a home.', 'related', title='Spanish learning')
        def connected(cfg, prompt):
            a, u = self.model(cfg, prompt)
            a['connections'] = [dict(record_id=related['record_id'], reason='Same vocabulary')]
            return a, u
        self.assertEqual(lib.run(self.core, record_id=self.rid, model_call=connected)['errors'][0]['code'], 'invalid_evidence')
        def valid(cfg, prompt):
            a, u = connected(cfg, prompt)
            a['evidence'].append(dict(record_id=related['record_id'], revision=1, quote='Spanish casa: a home.'))
            return a, u
        lib.run(self.core, record_id=self.rid, model_call=valid)
        self.core.correct(related['record_id'], 1, 'Spanish casa is house.', 'correction', 'related-change')
        self.assertTrue(lib.reports(self.core)['reports'][0]['stale'])

    def test_cli_returns_failure_for_unsaved_analysis(self):
        with patch('unanimis.librarian.complete', self.model), patch('unanimis.librarian.run', return_value={'errors': [{'code': 'model_error'}]}):
            with self.assertRaises(SystemExit) as error:
                main(['--data-dir', self.temp.name, 'librarian', 'run'])
        self.assertEqual(error.exception.code, 1)

    def test_obsidian_report_links_stale_banner_and_manual_edits_survive(self):
        from unanimis import wiki
        root = Path(self.temp.name)
        legacy = root / 'legacy'; legacy.mkdir()
        (legacy / 'notes').mkdir()
        (legacy / 'notes/source.md').write_text('Migration fixture')
        vault = root / 'reading-vault'
        wiki.migrate(self.core, wiki.plan(legacy), vault)
        report = lib.run(self.core, record_id=self.rid, model_call=self.model)['saved'][0]
        self.assertIn('Indexes/librarian', (vault / 'Home.md').read_text())
        index = vault / 'Indexes/librarian.md'
        self.assertIn('Librarian: Spanish learning', index.read_text())
        self.core.correct(self.rid, 1, 'Spanish casa means house. Updated.', 'human', 'view-change')
        self.assertIn('STALE', index.read_text())
        index.write_text('My manual review notes')
        refreshed = wiki.refresh(self.core)
        self.assertIn('Indexes/librarian.md', refreshed['conflicts'])
        self.assertEqual(index.read_text(), 'My manual review notes')

    def test_source_only_mode_sends_no_related_notes(self):
        self.core.store('Another Spanish learning note', 'related-private', title='Spanish learning')
        def source_only(cfg, prompt):
            self.assertEqual(prompt['related'], [])
            return self.model(cfg, prompt)
        result = lib.run(self.core, record_id=self.rid, related_limit=0, model_call=source_only)
        self.assertEqual(len(result['saved']), 1)
        with self.assertRaises(UnimError):
            lib.run(self.core, related_limit=4, model_call=self.model)

    def test_approved_report_retains_identity_and_drafts_use_reviewed_body(self):
        report_id = lib.run(self.core, record_id=self.rid, model_call=self.model)['saved'][0]['record_id']
        initial = self.core.get(report_id)
        reviewed = 'Reviewed summary: the writer plans practice, not completed practice.'
        evidence = initial['provenance']['metadata']['analysis']['evidence']
        proposal = self.core.propose(record_id=report_id, expected_revision=1, title=initial['title'], content=reviewed, summary='Correct tense', tags=[], evidence=evidence, request_id='review-report')
        self.core.approve(proposal['proposal_id'])
        current = self.core.get(report_id)
        self.assertNotIn('metadata', current['provenance'])
        status = lib.reports(self.core)['reports'][0]
        self.assertEqual(status['revision'], 2)
        self.assertEqual(status['acceptance'], 'accepted editorial revision')
        self.assertFalse(status['stale'])
        calls = self.calls
        self.assertEqual(lib.run(self.core, record_id=self.rid, model_call=self.model)['saved'], [])
        self.assertEqual(self.calls, calls)
        original_context = lib.metadata(self.core.get(report_id, 1), self.core)
        self.assertIn('summary', original_context['analysis'])
        self.assertNotIn('report_content', original_context['analysis'])
        def revised_draft(cfg, prompt):
            self.assertEqual(prompt['analysis']['report_content'], reviewed)
            self.assertNotIn('summary', prompt['analysis'])
            return dict(title='Spanish learning', content='I plan to practice Spanish casa.', summary='Clarify planned practice', tags=[], evidence=evidence), {}
        draft = lib.draft(self.core, report_id, model_call=revised_draft)
        self.assertEqual(draft['status'], 'pending')
        self.assertEqual(self.core.get(self.rid)['revision'], 1)
        self.core.correct(self.rid, 1, 'Spanish casa means house. Practiced today.', 'human change', 'after-report-approval')
        self.assertTrue(lib.reports(self.core)['reports'][0]['stale'])

    def test_repeated_approval_keeps_obsidian_report_and_tracks_new_dependencies(self):
        from unanimis import wiki
        root = Path(self.temp.name)
        legacy = root / 'legacy'; (legacy / 'notes').mkdir(parents=True)
        (legacy / 'notes/source.md').write_text('Migration fixture')
        vault = root / 'reading-vault'
        wiki.migrate(self.core, wiki.plan(legacy), vault)
        report_id = lib.run(self.core, record_id=self.rid, model_call=self.model)['saved'][0]['record_id']
        dependency = self.core.store('Practice remains planned.', 'new-reference')
        for revision in (1, 2):
            r = self.core.get(report_id)
            # Self-evidence documents the reason for a correction; it must not
            # make the newly approved report immediately stale against itself.
            evidence = [dict(record_id=report_id, revision=revision, quote=r['content'][:100]), dict(record_id=dependency['record_id'], revision=1, quote='Practice remains planned.')]
            p = self.core.propose(record_id=report_id, expected_revision=revision, title=r['title'], content='Reviewed body ' + str(revision), summary='Editorial clarification', tags=[], evidence=evidence, request_id='repeat-' + str(revision))
            self.core.approve(p['proposal_id'])
            index = (vault / 'Indexes/librarian.md').read_text()
            self.assertIn('Librarian: Spanish learning', index)
            self.assertIn('Accepted editorial revision', index)
            self.assertNotIn('STALE', index)
        self.assertEqual(lib.reports(self.core)['reports'][0]['revision'], 3)
        self.core.correct(dependency['record_id'], 1, 'Practice completed.', 'human change', 'dependency-update')
        status = lib.reports(self.core)['reports'][0]
        self.assertTrue(status['stale'])
        self.assertIn(dependency['record_id'], status['changed_sources'])
        self.assertIn('STALE', (vault / 'Indexes/librarian.md').read_text())
