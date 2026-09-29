"""Rebuildable local vector index; authoritative records remain in Core."""
import json
import math
import re
import sqlite3
import struct
from pathlib import Path
from .core import UnimError, now

MODEL = 'sentence-transformers/all-MiniLM-L6-v2'
SCHEMA = '''CREATE TABLE IF NOT EXISTS chunks(project TEXT,scope TEXT,record_id TEXT,revision INTEGER,chunk INTEGER,start INTEGER,content TEXT,vector BLOB,PRIMARY KEY(project,scope,record_id,chunk)); CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);'''
_MODELS = {}


def embed(core, texts, download=False):
    try:
        from fastembed import TextEmbedding
    except ImportError as e:
        raise UnimError('dependency_missing', 'Local embeddings require fastembed; install unanimis[semantic]') from e
    cache = str(core.data_dir / 'models/fastembed')
    key = (cache, download)
    if key not in _MODELS:
        _MODELS[key] = TextEmbedding(model_name=MODEL, cache_dir=cache, threads=2, local_files_only=not download)
    return [[float(x) for x in v] for v in _MODELS[key].embed(texts)]


def pieces(text, size=1400, overlap=200):
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        yield start, text[start:end]
        if end == len(text): break
        start = end-overlap


def index(core, embedder=embed):
    if core.read_only: raise UnimError('read_only', 'Indexing requires write access')
    path = core.data_dir/'semantic.sqlite3'
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    records = core.list_records()
    rows = []
    for r in records:
        for n,(start,chunk) in enumerate(pieces(r['content'])):
            rows.append((r,n,start,chunk))
    vectors = []
    for offset in range(0,len(rows),32):
        batch = rows[offset:offset+32]
        vectors.extend(embedder(core,[r['title']+'\n'+chunk for r,n,start,chunk in batch],download=True))
    if len(vectors)!=len(rows): raise UnimError('invalid_embedding','Embedding count mismatch')
    dims = len(vectors[0]) if vectors else 384
    if any(len(v)!=dims or not all(math.isfinite(x) for x in v) for v in vectors):
        raise UnimError('invalid_embedding','Invalid embedding dimensions or numbers')
    for r in records:
        if core.get(r['record_id'])['revision']!=r['revision']:
            raise UnimError('conflict','A source changed during indexing; rerun the index')
    with db:
        db.execute('DELETE FROM chunks WHERE project=? AND scope=?',(core.project,core.scope))
        for (r,n,start,chunk),v in zip(rows,vectors):
            db.execute('INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?)',(core.project,core.scope,r['record_id'],r['revision'],n,start,chunk,struct.pack('<%sf'%dims,*v)))
        db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',('model',MODEL))
        db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',('dimensions',str(dims)))
    db.close()
    return dict(model=MODEL,records=len(records),chunks=len(rows),indexed_at=now(),path=str(path),mode='local rebuildable index; no source revisions changed')


def rank(core, query, include_archived=False, embedder=embed):
    path=core.data_dir/'semantic.sqlite3'
    if not path.is_file(): raise UnimError('index_missing','Run unim search index before hybrid recall')
    db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
    try:
        cfg=dict(db.execute('SELECT key,value FROM settings'))
        if cfg.get('model')!=MODEL: raise UnimError('index_mismatch','Rebuild the semantic index with the current model')
        vector=embedder(core,[query])[0]
        dims=int(cfg['dimensions'])
        if len(vector)!=dims or not all(math.isfinite(x) for x in vector): raise UnimError('invalid_embedding','Query embedding mismatch')
        norm=math.sqrt(sum(x*x for x in vector)) or 1
        scored={};chunks={}; indexed=set();stale=0
        current={r['record_id']:r for r in core.list_records()}
        for rid,revision,start,content,blob in db.execute('SELECT record_id,revision,start,content,vector FROM chunks WHERE project=? AND scope=?',(core.project,core.scope)):
            r=current.get(rid)
            if r is None or (r['kind']=='archive' and not include_archived): continue
            if r['revision']!=revision:
                stale+=1;continue
            indexed.add(rid)
            if len(blob)!=dims*4: raise UnimError('index_mismatch','Corrupt vector; rebuild semantic index')
            v=struct.unpack('<%sf'%dims,blob)
            score=sum(a*b for a,b in zip(v,vector))/(norm*(math.sqrt(sum(a*a for a in v)) or 1))
            if score>=0.25 and score>scored.get(rid,-1):
                scored[rid]=score;chunks[rid]=dict(start=start,excerpt=content[:700],similarity=round(score,4))
        ids=sorted(scored,key=lambda rid:(-scored[rid],rid))[:50]
        eligible=sum(r['kind']!='archive' or include_archived for r in current.values())
        return ids,chunks,dict(model=MODEL,indexed_current_records=len(indexed),eligible_records=eligible,stale_chunks_skipped=stale,coverage_complete=len(indexed)==eligible)
    finally: db.close()


def source_name_ranks(core, query, include_archived=False):
    """An exact filename signal; provenance is a label, never a file to open.

    Collection names alone are not topic evidence. Inherited source_context
    retains the filename after revisions without editing immutable notes.
    """
    stop = set('a an and are as at be did do does for from had has have how i in is it me my of on or our that the their this to was we were what when where which who why will with would you'.split())
    stop.update(re.findall(r'\w+', (core.project + ' ' + core.scope).lower()))
    terms = set(re.findall(r'\w+', query.lower())) - stop
    if not terms:
        return {}
    matches = {}
    for record in core.list_records():
        if record['kind'] == 'archive' and not include_archived:
            continue
        source = record['source_context']['source']
        # Local source filenames only; do not infer filenames from URLs,
        # collection directories, log files or arbitrary provenance strings.
        if not isinstance(source, str) or not source.startswith('/'):
            continue
        path = Path(source)
        if path.suffix.lower() not in {'.md', '.txt', '.pdf', '.html', '.htm'}:
            continue
        words = set(re.findall(r'\w+', path.stem.lower()))
        overlap = terms & words
        if overlap:
            matches[record['record_id']] = len(overlap)
    # Equal filename matches receive the same vote, independent of UUID order.
    levels = sorted(set(matches.values()), reverse=True)
    return {rid: (levels.index(score), score / len(terms)) for rid, score in matches.items()}


def retrieve(core, query, lexical, mode='auto', include_archived=False):
    if mode not in ('auto','lexical','hybrid'): raise UnimError('invalid_input','mode must be auto, lexical or hybrid')
    if mode=='lexical' or (mode=='auto' and not (core.data_dir/'semantic.sqlite3').is_file()):
        return lexical,{},dict(mode='lexical')
    try:
        semantic,chunks,coverage=rank(core,query,include_archived)
    except Exception as error:
        if mode=='hybrid':
            if isinstance(error,UnimError):raise
            raise UnimError('semantic_unavailable','Local semantic search failed ('+type(error).__name__+'); use --mode lexical or rebuild the index') from error
        return lexical,{},dict(mode='lexical',warning='Semantic retrieval unavailable; using lexical search ('+type(error).__name__+').')
    # Weighted reciprocal-rank fusion preserves useful exact lexical hits.
    scores={}
    for weight,ranking in [(1.0,lexical[:50]),(1.0,semantic)]:
        for pos,rid in enumerate(ranking): scores[rid]=scores.get(rid,0)+weight/(30+pos+1)
    source_names = source_name_ranks(core, query, include_archived)
    for rid, (pos, coverage_fraction) in source_names.items():
        scores[rid] = scores.get(rid, 0) + coverage_fraction / (30 + pos + 1)
    ids=sorted(scores,key=lambda rid:(-scores[rid],rid))
    ids.extend(rid for rid in lexical if rid not in scores)
    return ids,chunks,dict(mode='hybrid',source_filename_matches=len(source_names),**coverage)
