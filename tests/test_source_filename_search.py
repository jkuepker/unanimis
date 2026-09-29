import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from unanimis.core import Core
from unanimis import semantic


class SourceFilenameSearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.core = Core(self.root / 'data')

    def tearDown(self):
        self.core.close()
        self.temp.cleanup()

    def test_source_note_beats_a_diagnostic_about_the_same_topic(self):
        source = self.core.store('I like the logo; connect the halves.', 'source',
                                 title='Generated with an image service',
                                 source='/nonexistent/Notes/Unanimis Logo ideas.md')
        diagnostic = self.core.store('Logo retrieval is broken. Why was retrieval broken?', 'diagnostic',
                                     title='Logo retrieval diagnostic',
                                     source='/nonexistent/baseline.log', metadata={'agent': 'Codex'})
        semantic.index(self.core, lambda core, texts, download=False: [[1., 0.] for _ in texts])
        order = [diagnostic['record_id'], source['record_id']]
        with patch('unanimis.semantic.rank', return_value=(order, {}, {'coverage_complete': True})):
            result = self.core.recall('logo', limit=1)
            self.assertEqual(result['matches'][0]['record_id'], source['record_id'])
            self.assertEqual(result['search']['source_filename_matches'], 1)
            # Diagnostics remain retrievable for their own subject.
            result = self.core.recall('why was retrieval broken', limit=1)
            self.assertEqual(result['matches'][0]['record_id'], diagnostic['record_id'])
            # Paging keeps both records; the diagnostic was not hidden.
            result = self.core.recall('logo', limit=1, offset=1)
            self.assertEqual(result['matches'][0]['record_id'], diagnostic['record_id'])

    def test_inherited_source_labels_honor_scope_and_archive_filter(self):
        source = self.core.store('Original words.', 'source', source='/Notes/Logo.md')
        self.core.correct(source['record_id'], 1, 'Revised words.', 'User edit', 'edit')
        archived = self.core.store('An old logo.', 'archive', source='/Notes/Old logo.md', kind='archive')
        other = Core(self.root / 'data', scope='other')
        try:
            private = other.store('Private logo.', 'private', source='/Notes/Private logo.md')
        finally:
            other.close()
        ranks = semantic.source_name_ranks(self.core, 'logo')
        self.assertIn(source['record_id'], ranks)
        self.assertNotIn(archived['record_id'], ranks)
        self.assertNotIn(private['record_id'], ranks)
        self.assertIn(archived['record_id'], semantic.source_name_ranks(self.core, 'logo', True))
        self.assertEqual(self.core.get(source['record_id'])['revision'], 2)
        self.assertEqual(self.core.get(source['record_id'], 1)['content'], 'Original words.')

    def test_topic_signal_is_exact_and_query_specific(self):
        source = self.core.store('A note.', 'source', source='/Notes/Unanimis Logo.md')
        self.core.store('A directory.', 'directory', source='/Notes/Logo')
        self.core.store('A URL.', 'url', source='https://example.test/Logo.md')
        self.core.store('A log file.', 'logfile', source='/Notes/Logo.log')
        self.assertEqual(semantic.source_name_ranks(self.core, 'unanimis'), {})
        self.assertEqual(semantic.source_name_ranks(self.core, 'log'), {})
        ranks = semantic.source_name_ranks(self.core, 'logo')
        self.assertEqual(set(ranks), {source['record_id']})
        specific = ranks[source['record_id']][1]
        broad = semantic.source_name_ranks(self.core, 'why does hybrid recall bury the logo result for a typo')[source['record_id']][1]
        self.assertLess(broad, specific)

    def test_filename_only_candidate_and_lexical_mode(self):
        source = self.core.store('A drawing.', 'source', source='/Notes/Logo.md')
        semantic.index(self.core, lambda core, texts, download=False: [[1., 0.] for _ in texts])
        with patch('unanimis.semantic.rank', return_value=([], {}, {'coverage_complete': True})):
            self.assertEqual(self.core.recall('logo')['matches'][0]['record_id'], source['record_id'])
            self.assertEqual(self.core.recall('logo', mode='lexical')['matches'], [])


if __name__ == '__main__':
    unittest.main()
