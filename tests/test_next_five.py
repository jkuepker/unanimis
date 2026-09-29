import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch
import zipfile
from unanimis.core import Core,UnimError
from unanimis import semantic,backup,quality,ingest,librarian,wiki


class NextFiveTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.core=Core(self.root/'data')
    def tearDown(self):self.core.close();self.temp.cleanup()
    def embedding(self,core,texts,download=False):return [[1.,0.] if 'name' in t.lower() else [0.,1.] for t in texts]

    def test_local_index_scope_and_revision_filtering(self):
        source=self.core.store('The product name is unanimis.','name',title='Naming')
        semantic.index(self.core,self.embedding)
        ids,chunks,status=semantic.rank(self.core,'name',embedder=self.embedding)
        self.assertEqual(ids,[source['record_id']]);self.assertTrue(status['coverage_complete'])
        other=Core(self.root/'data',scope='other');private=other.store('A private name','private');semantic.index(other,self.embedding);other.close()
        ids,_,_=semantic.rank(self.core,'name',embedder=self.embedding);self.assertNotIn(private['record_id'],ids)
        self.core.correct(source['record_id'],1,'The updated name is unanimis.','update','rev2')
        ids,_,status=semantic.rank(self.core,'name',embedder=self.embedding)
        self.assertEqual(ids,[]);self.assertFalse(status['coverage_complete']);self.assertGreater(status['stale_chunks_skipped'],0)
        semantic.index(self.core,self.embedding)
        ids,_,status=semantic.rank(self.core,'name',embedder=self.embedding);self.assertEqual(ids,[source['record_id']])

    def test_hybrid_fusion_and_explicit_fallback(self):
        source=self.core.store('A name','one');second=self.core.store('Second document','two')
        semantic.index(self.core,self.embedding)
        with patch('unanimis.semantic.rank',return_value=([second['record_id']],{second['record_id']:{'excerpt':'Second document'}},{'coverage_complete':True})):
            ids,chunks,info=semantic.retrieve(self.core,'query',[source['record_id']])
            self.assertEqual(set(ids),{source['record_id'],second['record_id']});self.assertEqual(info['mode'],'hybrid')
        with patch('unanimis.semantic.rank',side_effect=RuntimeError('offline')):
            ids,_,info=semantic.retrieve(self.core,'query',[source['record_id']])
            self.assertEqual(ids,[source['record_id']]);self.assertIn('warning',info)
            with self.assertRaises(UnimError):semantic.retrieve(self.core,'query',[],mode='hybrid')

    def prepare_vault(self):
        legacy=self.root/'legacy';(legacy/'notes').mkdir(parents=True)
        (legacy/'notes/original.md').write_text('Original fixture')
        vault=self.root/'vault';wiki.migrate(self.core,wiki.plan(legacy),vault)
        (vault/'Notes/human.md').write_text('Human text')
        (vault/'attachments').mkdir(exist_ok=True);(vault/'attachments/image.bin').write_bytes(b'fixture-image')
        return vault

    def test_backup_restore_preserves_history_attachments_and_disconnects_legacy(self):
        self.prepare_vault()
        r=self.core.store('Old text','r');self.core.correct(r['record_id'],1,'New text','human','rev2')
        target=self.root/'snapshot.zip';made=backup.create(self.core,target)
        self.assertTrue(made['verified']);self.assertTrue(backup.verify(target)['verified'])
        restored=backup.restore(target,self.root/'restored')
        import os,subprocess,sys
        env=dict(os.environ);env.pop('PYTHONPATH',None)
        proc=subprocess.run([sys.executable,restored['launcher'],'--json','get',r['record_id']],cwd=restored['restored'],env=env,text=True,capture_output=True,timeout=15)
        self.assertEqual(proc.returncode,0,proc.stderr);self.assertEqual(json.loads(proc.stdout)['content'],'New text')
        c=Core(Path(restored['data_dir']),read_only=True)
        self.assertEqual(c.get(r['record_id'],1)['content'],'Old text');self.assertEqual(c.get(r['record_id'])['content'],'New text');c.close()
        self.assertEqual((self.root/'restored/vault/Notes/human.md').read_text(),'Human text')
        self.assertEqual((self.root/'restored/vault/attachments/image.bin').read_bytes(),b'fixture-image')
        config=json.loads((self.root/'restored/data/wiki.json').read_text())
        self.assertEqual(config['vault'],str(self.root/'restored/vault'));self.assertNotEqual(config['legacy_source'],str(self.root/'legacy'))
        with self.assertRaises(UnimError):backup.restore(target,self.root/'restored')
        with self.assertRaises(UnimError):backup.create(self.core,target)

    def test_backup_rejects_tampering_symlinks_and_traversal(self):
        self.core.store('Source','source')
        (self.root/'data/link').symlink_to(self.root/'data/unanimis.sqlite3')
        with self.assertRaises(UnimError):backup.create(self.core,self.root/'no.zip')
        (self.root/'data/link').unlink()
        path=self.root/'bad.zip'
        with zipfile.ZipFile(path,'w') as z:z.writestr('../escape','bad');z.writestr('manifest.json','{}')
        with self.assertRaises(UnimError):backup.restore(path,self.root/'bad-restore')
        self.assertFalse((self.root/'bad-restore').exists())
        good=self.root/'good.zip';backup.create(self.core,good)
        changed=self.root/'changed.zip'
        with zipfile.ZipFile(good) as source,zipfile.ZipFile(changed,'w') as dest:
            for name in source.namelist():dest.writestr(name,b'corrupted' if name.endswith('.sqlite3') else source.read(name))
        with self.assertRaises(UnimError):backup.verify(changed)

    def report_fixture(self):
        librarian.configure(self.core,'http://localhost:8000/v1','fixture')
        source=self.core.store('Only three runs were measured.','source')
        report=self.core.store('# Report\n\n## Summary\n\nEvery possible model always loses.','report',kind='review',metadata=dict(librarian_version='librarian-v1',target=dict(record_id=source['record_id'],revision=1),analysis=dict(evidence=[dict(record_id=source['record_id'],revision=1,quote='Only three runs were measured.')])) )
        return source,report

    def judge(self,config,prompt):
        report,source=prompt['records'][:2]
        return dict(findings=[dict(claim_id='summary',assessment='overgeneralized',reason='Three measured runs do not establish every possible model.',evidence=[dict(record_id=report['record_id'],revision=report['revision'],quote='Every possible model always loses.'),dict(record_id=source['record_id'],revision=source['revision'],quote=source['content'])])]),{}

    def test_quality_check_is_advisory_idempotent_and_tracks_evidence(self):
        source,report=self.report_fixture()
        checked=quality.check(self.core,report['record_id'],self.judge)
        self.assertEqual(checked['issues'],1);self.assertEqual(self.core.get(report['record_id'])['revision'],1)
        self.assertEqual(self.core.list_proposals()['proposals'],[])
        self.assertTrue(quality.check(self.core,report['record_id'],lambda *a: self.fail('should reuse'))['cached'])
        saved=self.core.get(checked['saved']['record_id']);self.assertEqual(saved['freshness']['status'],'references_current')
        self.core.correct(source['record_id'],1,'Three more runs were measured.','update','changed')
        self.assertEqual(self.core.get(saved['record_id'])['freshness']['status'],'stale')
        with self.assertRaises(UnimError):quality.check(self.core,report['record_id'],self.judge)

    def test_quality_rejects_missing_claims_and_invented_evidence(self):
        source,report=self.report_fixture()
        with self.assertRaises(UnimError):quality.check(self.core,report['record_id'],lambda *a:({'findings':[]},{}))
        def bad(cfg,prompt):
            result,usage=self.judge(cfg,prompt);result['findings'][0]['evidence'][1]['quote']='fabricated';return result,usage
        with self.assertRaises(UnimError):quality.check(self.core,report['record_id'],bad)
        self.assertEqual(len(self.core.list_records()),2)

    def test_html_text_import_preserves_originals_and_deduplicates(self):
        path=self.root/'article.html';raw=b'<html><head><title>Useful article</title></head><body><nav>Menu</nav><main><p>Useful knowledge.</p><script>steal()</script></main></body></html>';path.write_bytes(raw)
        result=ingest.ingest(self.core,str(path));r=self.core.get(result['record_id'])
        self.assertEqual(r['title'],'Useful article');self.assertEqual(r['content'],'Useful knowledge.')
        self.assertEqual(Path(result['original']).read_bytes(),raw)
        self.assertEqual(ingest.ingest(self.core,str(path))['record_id'],result['record_id'])
        note=self.root/'note.md';note.write_text('My exact words.\n\nAnother thought.\n')
        imported=ingest.ingest(self.core,str(note));self.assertEqual(self.core.get(imported['record_id'])['content'],note.read_text())

    def test_pdf_reader_reports_pages_and_public_network_boundary(self):
        class Reader:
            is_encrypted=False;metadata={'/Title':'Paper'}
            def __init__(self,*args,**kwargs):self.pages=[types.SimpleNamespace(extract_text=lambda:'Page one evidence.'),types.SimpleNamespace(extract_text=lambda:'')]
        with patch.dict('sys.modules',{'pypdf':types.SimpleNamespace(PdfReader=Reader)}):
            title,text,meta,ext=ingest.extract(b'%PDF-fixture','paper.pdf')
            self.assertEqual(title,'Paper');self.assertEqual(meta['pages'],2);self.assertEqual(meta['empty_pages'],[2]);self.assertIn('Page one evidence.',text)
        with patch('unanimis.ingest.socket.getaddrinfo',return_value=[(None,None,None,None,('127.0.0.1',80))]):
            with self.assertRaisesRegex(UnimError,'public network'):ingest.fetch('http://example.invalid/article')

    def test_cli_shows_semantically_matched_passage_outside_opening_excerpt(self):
        import contextlib,io
        from unanimis.cli import main
        body='Opening context. '*120+'Fermentation produces the loaf.'
        record=self.core.store(body,'long',title='Long source')
        semantic.index(self.core,self.embedding)
        match={record['record_id']:dict(start=2040,excerpt='Fermentation produces the loaf.',similarity=0.7)}
        with patch('unanimis.semantic.rank',return_value=([record['record_id']],match,{'coverage_complete':True})):
            out=io.StringIO()
            with contextlib.redirect_stdout(out):main(['--data-dir',str(self.root/'data'),'recall','bread'])
        self.assertIn('Matched passage',out.getvalue());self.assertIn('Fermentation produces the loaf.',out.getvalue())
        self.assertEqual(self.core.get(record['record_id'])['content'],body)

    def test_quality_source_passage_ids_expand_to_exact_citations(self):
        source,report=self.report_fixture()
        def selected(cfg,prompt):
            key=next(iter(prompt['source_passages']))
            return {'findings':[dict(claim_id='summary',assessment='overgeneralized',reason='Only three measured runs.',source_quotes=[key])]},{}
        checked=quality.check(self.core,report['record_id'],selected)
        refs=checked['findings'][0]['evidence']
        self.assertEqual({x['record_id'] for x in refs},{source['record_id'],report['record_id']})
        self.assertEqual(next(x['quote'] for x in refs if x['record_id']==source['record_id']),'Only three runs were measured.')

    def test_quality_checks_each_sentence_instead_of_approving_a_whole_paragraph(self):
        text='# Report\n\n## Summary\n\nThree runs were measured. Every possible model always loses.\n\nCategory: finding\nTags: inference\n\n## Evidence\n'
        parsed=quality.claims({'content':text})
        self.assertEqual([x['id'] for x in parsed],['summary-1','summary-2'])
        self.assertEqual(parsed[1]['text'],'Every possible model always loses.')
