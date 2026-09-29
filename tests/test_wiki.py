import json
import tempfile
import unittest
from pathlib import Path

from unanimis.core import Core, UnimError, digest
from unanimis import wiki


class WikiTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.old=self.root/'old'
        (self.old/'findings').mkdir(parents=True)
        (self.old/'inbox/processed').mkdir(parents=True)
        self.original='---\ntitle: "A source"\n---\n\nExact measurement 46 / 14. [[second]]\n'
        (self.old/'findings/first.md').write_text(self.original)
        (self.old/'findings/second.md').write_text('# Second\nA linked source.')
        (self.old/'inbox/processed/raw.md').write_text('Rawunique transcript evidence 46 / 14.')
        self.core=Core(self.root/'data')
        self.vault=self.root/'vault'

    def tearDown(self):
        self.core.close(); self.temp.cleanup()

    def migrate(self):
        return wiki.migrate(self.core,wiki.plan(self.old),self.vault)

    def test_legacy_frontmatter_is_preserved_in_record_and_reading_view(self):
        text='---\ntitle: "Tagged note"\ntags: [lang/spanish, learning]\naliases: [phrase]\ncustom: retained\n---\n\nOriginal text.\n'
        (self.old/'notes').mkdir()
        (self.old/'notes/tagged.md').write_text(text)
        self.migrate()
        record=self.core.recall('Tagged')['matches'][0]
        self.assertEqual(self.core.get(record['record_id'])['content'],text)
        view=(self.vault/'Library/notes/tagged.md').read_text()
        self.assertIn('tags: [lang/spanish, learning]',view)
        self.assertIn('aliases: [phrase]',view)
        self.assertIn('custom: retained',view)

    def test_migration_reuses_pilot_identity_and_preserves_accepted_revision(self):
        old=self.core.store(self.original,'pilot',source='/pilot/first.md',metadata={'original_path':str(self.old/'findings/first.md')})
        self.core.correct(old['record_id'],1,'# Accepted narrower finding\n46 / 14 in the stated fixture.','Explicit correction','correction')
        result=self.migrate()
        self.assertEqual(result['reused'],1)
        self.assertEqual(len(self.core.list_records()),3)
        self.assertEqual(self.core.get(old['record_id'])['revision'],2)
        self.assertEqual(self.core.get(old['record_id'],1)['content'],self.original)
        self.assertIn('Accepted narrower finding',(self.vault/'Library/findings/first.md').read_text())
        self.assertTrue((self.vault/('Archive/History/'+old['record_id']+'/r0001.md')).exists())
        self.assertEqual((self.old/'findings/first.md').read_text(),self.original)
        self.migrate()
        self.assertEqual(len(self.core.list_records()),3)

    def test_noncanonical_plan_root_reuses_records_and_inbox_receipts(self):
        pending=self.old/'inbox/pending.md'; pending.write_text('Pending source')
        self.core.store(self.original,'pilot',metadata={'original_path':str(self.old/'findings/first.md')})
        value=wiki.plan(self.old)
        alias=self.root/'alias'; alias.symlink_to(self.old,target_is_directory=True)
        value['source']=str(alias)
        result=wiki.migrate(self.core,value,self.vault)
        self.assertEqual(result['reused'],1)
        self.assertEqual(wiki.sync(self.core)['saved'],[])
        self.assertEqual(len(self.core.list_records()),4)

    def test_migration_rejects_changed_plan_before_writing(self):
        plan=wiki.plan(self.old)
        (self.old/'findings/first.md').write_text('Changed after plan')
        with self.assertRaisesRegex(UnimError,'changed since planning'):
            wiki.migrate(self.core,plan,self.vault)
        self.assertEqual(self.core.list_records(),[])

    def test_migration_rejects_divergent_pilot_before_writing(self):
        self.core.store('Different original','pilot',metadata={'original_path':str(self.old/'findings/first.md')})
        with self.assertRaisesRegex(UnimError,'diverged'):
            self.migrate()
        self.assertEqual(len(self.core.list_records()),1)

    def test_archives_are_explicitly_searchable_and_links_resolve(self):
        self.migrate()
        self.assertEqual(self.core.recall('Rawunique')['matches'],[])
        archived=self.core.recall('Rawunique',include_archived=True)['matches']
        self.assertEqual(len(archived),1)
        self.assertEqual(self.core.get(archived[0]['record_id'])['content'],'Rawunique transcript evidence 46 / 14.')
        self.assertIn('[[Library/findings/second|second]]',(self.vault/'Library/findings/first.md').read_text())

    def test_manual_projection_edit_is_preserved_and_reported(self):
        self.migrate()
        path=self.vault/'Library/findings/first.md'
        path.write_text('My unsynced manual edit')
        result=self.core.store('Another capture','another')
        self.assertIn('view_warning',result)
        self.assertEqual(path.read_text(),'My unsynced manual edit')
        self.assertIn('Library/findings/first.md',wiki.config(self.core)['conflicts'])

    def test_human_notes_capture_verbatim_revise_and_revert_without_duplicates(self):
        self.migrate()
        path=self.vault/'Notes/spanish.md'; text='  Aprendo español.\nA useful phrase.\n'
        path.write_text(text)
        first=wiki.sync(self.core)['saved'][0]
        self.assertEqual(self.core.get(first['record_id'])['content'],text)
        self.assertEqual(wiki.sync(self.core)['saved'],[])
        for n,new in enumerate(['Version B',text,'Version B'],2):
            path.write_text(new); wiki.sync(self.core)
            self.assertEqual(self.core.get(first['record_id'])['revision'],n)
            self.assertEqual(self.core.get(first['record_id'])['content'],new)
        self.assertEqual(path.read_text(),'Version B')
        self.assertEqual(len(self.core.list_records()),4)

    def test_note_sync_does_not_overwrite_a_competing_agent_revision(self):
        self.migrate()
        path=self.vault/'Notes/idea.md'; path.write_text('Original human note')
        record=wiki.sync(self.core)['saved'][0]
        self.core.correct(record['record_id'],1,'A separate accepted correction','User authorized','other')
        path.write_text('New local edit')
        result=wiki.sync(self.core)
        self.assertEqual(len(result['errors']),1)
        self.assertEqual(self.core.get(record['record_id'])['content'],'A separate accepted correction')
        self.assertEqual(path.read_text(),'New local edit')

    def test_note_sync_recovers_committed_write_before_binding_save(self):
        self.migrate()
        path=self.vault/'Notes/note.md'; path.write_text('A')
        record=wiki.sync(self.core)['saved'][0]
        path.write_text('B')
        self.core.correct(record['record_id'],1,'B','User-controlled Notes file changed: Notes/note.md',
                          'wiki-note-edit:'+digest(str(path.resolve()))+':r1:'+digest('B'))
        result=wiki.sync(self.core)
        self.assertEqual(result['errors'],[])
        self.assertEqual(wiki.config(self.core)['notes']['Notes/note.md']['revision'],2)
        self.assertEqual(self.core.get(record['record_id'])['revision'],2)

    def test_pending_drop_import_is_not_recaptured_by_bridge(self):
        (self.old/'inbox/pending.md').write_text('A pending capture')
        self.migrate()
        self.assertEqual(len(self.core.list_records()),4)
        self.assertEqual(wiki.sync(self.core)['saved'],[])
        self.assertEqual(len(self.core.list_records()),4)

    def test_legacy_inbox_bridge_is_idempotent_and_preserves_input(self):
        self.migrate()
        path=self.old/'inbox/new.md'; path.write_text('Raw pending content')
        self.assertEqual(len(wiki.sync(self.core)['saved']),1)
        self.assertEqual(wiki.sync(self.core)['saved'],[])
        self.assertEqual(path.read_text(),'Raw pending content')

    def test_symlinks_and_existing_nonempty_destination_are_rejected(self):
        outside=self.root/'outside.md'; outside.write_text('Outside')
        (self.old/'findings/link.md').symlink_to(outside)
        with self.assertRaises(UnimError): wiki.plan(self.old)
        (self.old/'findings/link.md').unlink()
        self.vault.mkdir(); (self.vault/'mine.md').write_text('Keep me')
        with self.assertRaisesRegex(UnimError,'must be empty'): self.migrate()
        self.assertEqual((self.vault/'mine.md').read_text(),'Keep me')

    def test_approved_review_page_is_retired_not_left_pending(self):
        self.migrate()
        target=self.core.recall('measurement')['matches'][0]
        result=self.core.write('Narrowed finding','edit',record_id=target['record_id'],expected_revision=1,
                               evidence=[{'record_id':target['record_id'],'revision':1,'quote':'46 / 14'}])
        path=self.vault/('Review/'+result['proposal_id']+'.md')
        self.assertTrue(path.exists())
        self.core.approve(result['proposal_id'])
        self.assertFalse(path.exists())
        self.assertTrue(list((self.vault/'Archive/Retired').glob('*'+result['proposal_id']+'.md')))
        self.assertIn('No pending editorial proposals.',(self.vault/'Home.md').read_text())


if __name__=='__main__': unittest.main()
