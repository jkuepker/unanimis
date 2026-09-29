"""Lossless legacy import and an owned-file Obsidian view. No model or scheduler calls."""
import fcntl
import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .core import UnimError, digest, now, title_of

CATEGORIES = ("findings", "notes", "projects", "maps", "sources")


def atomic(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".unim-")
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(content if isinstance(content, bytes) else content.encode("utf-8"))
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def config_path(core):
    return core.data_dir / "wiki.json"


def config(core):
    p = config_path(core)
    if not p.exists():
        raise UnimError("not_configured", "Run unim wiki migrate with a reviewed plan first")
    value = json.loads(p.read_text())
    core.check_scope(value["project"], value["scope"])
    return value


@contextmanager
def locked(core):
    with (core.data_dir / ".wiki.lock").open("a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def safe_file(root, relative):
    root = Path(root).resolve()
    path = root / relative
    if Path(relative).is_absolute() or ".." in Path(relative).parts or root not in path.resolve().parents:
        raise UnimError("invalid_path", "Path must stay inside its configured root")
    if any(p.is_symlink() for p in [path] + list(path.parents) if p != root and root in p.parents):
        raise UnimError("invalid_path", "Symlinks are not imported or overwritten")
    return path


def plan(source):
    root = Path(source).resolve()
    if not root.is_dir():
        raise UnimError("not_found", "Wiki source directory does not exist")
    entries = []
    for folder in list(CATEGORIES) + ["inbox/processed", "inbox/quarantine"]:
        for p in sorted((root / folder).rglob("*.md")):
            relative = p.relative_to(root).as_posix()
            p = safe_file(root, relative)
            raw = p.read_bytes()
            text = raw.decode("utf-8")
            if not text.strip() or len(text) > 200000:
                raise UnimError("invalid_input", "Unsupported empty/oversized source: " + relative)
            entries.append(dict(path=relative, sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw),
                                kind="archive" if folder.startswith("inbox/") else "source"))
    for p in sorted((root / "inbox").glob("*.md")):
        if p.name == "README.md":
            continue
        raw = safe_file(root, p.relative_to(root).as_posix()).read_bytes()
        raw.decode("utf-8")
        entries.append(dict(path=p.relative_to(root).as_posix(), sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw), kind="capture"))
    return dict(version=1, source=str(root), created_at=now(), entries=entries,
                curated=sum(e["kind"] == "source" for e in entries), archived=sum(e["kind"] == "archive" for e in entries),
                pending=sum(e["kind"] == "capture" for e in entries),
                policy="Import exact UTF-8 text; reuse matching existing records; preserve accepted revisions. Metadata/config/Git remain in the full backup.")


def validate_plan(value):
    if value.get("version") != 1 or not value.get("entries"):
        raise UnimError("invalid_input", "Expected a nonempty version-1 migration plan")
    current = plan(value["source"])
    if current["entries"] != value["entries"]:
        raise UnimError("conflict", "Wiki content changed since planning; make a new plan before cutover")
    return current


def original_path(record):
    provenance = record["provenance"]
    return provenance.get("metadata", {}).get("original_path") or provenance.get("source")


def migrate(core, value, vault):
    validate_plan(value)
    vault = Path(vault).resolve()
    source = Path(value["source"]).resolve()
    if vault == source or source in vault.parents or vault in source.parents:
        raise UnimError("invalid_path", "Replacement vault must be separate from the original wiki")
    with locked(core):
        if config_path(core).exists():
            state = config(core)
            if state["vault"] != str(vault) or state["legacy_source"] != str(source):
                raise UnimError("conflict", "This store already has a different wiki binding")
        else:
            if vault.exists() and any(vault.iterdir()):
                raise UnimError("conflict", "Replacement vault must be empty on first migration")
            state = dict(version=1, project=core.project, scope=core.scope, vault=str(vault), legacy_source=str(source),
                         bindings={}, managed={}, notes={}, inbox={}, conflicts=[], migrated_at=now())
        by_source = {}
        for record in core.list_records():
            origins = {original_path(core.get(record['record_id'], n)) for n in range(1, record['current_revision']+1)}
            for origin in origins - {None}:
                by_source.setdefault(str(Path(origin).resolve()), []).append(record)
        # Preflight every source before creating any record. Existing accepted edits win.
        reuse = {}
        for entry in value["entries"]:
            origin = str(source / entry["path"])
            candidates = by_source.get(origin, [])
            if len(candidates) > 1:
                raise UnimError("conflict", "Multiple records claim source " + entry["path"])
            if candidates:
                candidate = candidates[0]
                matching = [n for n in range(1, candidate["current_revision"] + 1)
                            if digest(core.get(candidate["record_id"], n)["content"]) == entry["sha256"]]
                if not matching:
                    raise UnimError("conflict", "Legacy source diverged from all known revisions: " + entry["path"])
                reuse[entry["path"]] = (candidate["record_id"], matching[-1])
        core._view_suspended = True
        imported, reused = 0, 0
        try:
            for entry in value["entries"]:
                origin = source / entry["path"]
                raw = safe_file(source, entry["path"]).read_bytes()
                if hashlib.sha256(raw).hexdigest() != entry["sha256"]:
                    raise UnimError("conflict", "Source changed during migration: " + entry["path"])
                if entry["path"] in reuse:
                    record_id, source_revision = reuse[entry["path"]]
                    reused += 1
                else:
                    result = core.store(raw.decode("utf-8"), "wiki-import:" + digest(str(origin)), source=str(origin), kind=entry["kind"],
                                        title=title_of(body(raw.decode("utf-8"))) if entry["kind"] == "archive" else None,
                                        metadata={"original_path": str(origin), "sha256": entry["sha256"], "legacy_path": entry["path"],
                                                  "status": "archived raw capture" if entry["kind"] == "archive" else "imported source; claims not independently verified"})
                    if result.get("export_warning"):
                        raise UnimError("export_failed", result["export_warning"])
                    record_id, source_revision = result["record_id"], result["revision"]
                    imported += 1
                category = entry["path"].split('/')[0]
                view = "Archive/Captures/" + Path(entry["path"]).name if entry["kind"] == "archive" else "Library/" + entry["path"]
                state["bindings"][record_id] = dict(path=view, original=entry["path"], source_hash=entry["sha256"], source_revision=source_revision)
                if entry['kind'] == 'capture' and entry['path'].startswith('inbox/'):
                    state['inbox'][str(origin)] = entry['sha256']
            validate_plan(value)
            vault.mkdir(parents=True, exist_ok=True)
            (vault / "Notes").mkdir(exist_ok=True)
            helpfile = vault / "Notes/README.md"
            if not helpfile.exists():
                atomic(helpfile, "# Your notes\n\nCreate ordinary Markdown notes here: language learning, articles, project notes, or anything you want to retain. Run `unim wiki sync` to capture new notes and append revisions for changed notes. Your wording stays intact.\n\n`Library/` is a generated reading view. Ask an agent to propose changes there; manual edits are preserved and reported as conflicts, never silently overwritten.\n")
            atomic(config_path(core), json.dumps(state, indent=2))
        finally:
            core._view_suspended = False
        view = refresh(core, already_locked=True)
        return dict(imported=imported, reused=reused, total_sources=len(value["entries"]), vault=str(vault), view=view)


def slug(title):
    return re.sub(r"[^\w-]+", "-", title.lower()).strip('-')[:70] or "note"


def body(text):
    return re.sub(r"\A---\r?\n.*?\r?\n---\r?\n", "", text, count=1, flags=re.S).lstrip('\r\n')


def render_page(record, aliases):
    def link(match):
        raw = match.group(1)
        target, sep, label = raw.partition('|')
        name, hashmark, anchor = target.partition('#')
        mapped = aliases.get(name) or aliases.get(Path(name).stem)
        if not mapped:
            return match.group(0)
        suffix = '#' + anchor if hashmark else ''
        return '[[' + mapped.removesuffix('.md') + suffix + '|' + (label if sep else name) + ']]'
    text = re.sub(r"\[\[([^\]]+)\]\]", link, body(record["content"]))
    original_header = re.match(r"\A---\r?\n(.*?)\r?\n---\r?\n", record['content'], flags=re.S)
    fields = original_header.group(1) if original_header else 'title: ' + json.dumps(record['title'], ensure_ascii=False)
    fields = re.sub(r'(?m)^unim_(?:record_id|revision|kind|state):[^\n]*\n?', '', fields).strip()
    if record['tags'] or record['provenance'].get('proposal_id'):
        tags = 'tags: ' + json.dumps(record['tags'], ensure_ascii=False)
        fields = re.sub(r'(?m)^tags:[^\n]*(?:\n[ \t]+[^\n]*)*', tags, fields) if re.search(r'(?m)^tags:', fields) else fields + '\n' + tags
    managed = '\n'.join('unim_' + k + ': ' + json.dumps(record[k], ensure_ascii=False) for k in ['record_id','revision','kind','state'])
    header = '---\n' + fields + '\n' + managed + '\n---\n\n'
    banner = '> Historical revision; superseded.\n\n' if record['state'] == 'superseded' else ''
    provenance = '\n\n---\nRecord: `' + record['record_id'] + '` · revision ' + str(record['revision']) + '\n\nStatus: ' + str(record['acceptance']) + '. Evidence: ' + record['evidence_verification'] + '.\n'
    if record['current_revision'] > 1 and record['state'] == 'current':
        provenance += '\nHistory: ' + ' · '.join('[[Archive/History/' + record['record_id'] + '/r%04d|Revision %d]]' % (n,n) for n in range(1, record['current_revision'])) + '\n'
    return header + banner + text + provenance


def refresh(core, already_locked=False):
    if not config_path(core).exists() or getattr(core, '_view_suspended', False):
        return dict(enabled=False)
    if not already_locked:
        with locked(core):
            return refresh(core, already_locked=True)
    state = config(core)
    vault = Path(state['vault'])
    records = core.list_records()
    paths, aliases = {}, {}
    owned = {v['record_id']: name for name,v in state['notes'].items()}
    for record in records:
        rid = record['record_id']
        paths[rid] = owned.get(rid) or state['bindings'].get(rid, {}).get('path') or ('Archive/Captures/' if record['kind']=='archive' else 'Library/Captures/') + slug(record['title']) + '-' + rid[:8] + '.md'
        binding = state['bindings'].get(rid)
        if binding:
            for key in [binding['original'], str(Path(binding['original']).with_suffix('')), Path(binding['original']).stem]:
                if key in aliases and aliases[key] != paths[rid]:
                    aliases[key] = None
                else:
                    aliases[key] = paths[rid]
    desired = {}
    for record in records:
        if record['record_id'] not in owned:
            desired[paths[record['record_id']]] = render_page(record, aliases)
        for n in range(1,record['current_revision']):
            historical = core.get(record['record_id'], n)
            desired['Archive/History/' + record['record_id'] + '/r%04d.md' % n] = render_page(historical,aliases)
    categories = {}
    for record in records:
        if record['kind'] == 'archive': continue
        target = paths[record['record_id']]
        category = target.split('/')[1] if target.startswith('Library/') else 'Your notes'
        categories.setdefault(category,[]).append((record['title'],target))
    for category, items in categories.items():
        desired['Indexes/' + slug(category) + '.md'] = '# ' + category + '\n\n' + '\n'.join('- [[' + target.removesuffix('.md') + '|' + title.replace('|',' ') + ']]' for title,target in sorted(items)) + '\n'
    proposals = core.list_proposals()['proposals']
    for proposal in proposals:
        p = proposal['payload']
        desired['Review/' + proposal['id'] + '.md'] = '# Pending: ' + p['title'] + '\n\nTarget: `' + proposal['record_id'] + '` revision ' + str(proposal['expected_revision']) + '\n\n' + p['summary'] + '\n\n## Draft\n\n' + p['content'] + '\n\n## Evidence\n\n' + '\n'.join('- `' + c['record_id'] + '` revision ' + str(c['revision']) + ': ' + c['quote'] for c in p['evidence']) + '\n\nAfter review: `unim librarian approve ' + proposal['id'] + '`\n'
    from .librarian import VERSION, metadata, report_status
    librarian_reports = [r for r in records if metadata(r, core).get('librarian_version') == VERSION]
    report_links = []
    for report in librarian_reports:
        status = report_status(core, report)
        target = status['target']
        link = paths[report['record_id']].removesuffix('.md')
        warning = ('Evidence freshness unavailable; inspect cited sources.' if status['unavailable'] else
                   'STALE — cited source changed; rerun librarian.' if status['stale'] else
                   'Accepted editorial revision; claims are not independently verified.' if status['acceptance'] == 'accepted editorial revision' else
                   'Proposed analysis; review before relying on it.')
        source_link = paths[target['record_id']].removesuffix('.md')
        desired[paths[report['record_id']]] = '# ' + warning + '\n\nSource: [[' + source_link + '|Original note]] revision ' + str(target['revision']) + '\n\n' + desired[paths[report['record_id']]]
        report_links.append('- [[' + link + '|' + report['title'].replace('|', ' ') + ']] — ' + warning)
    if librarian_reports:
        desired['Indexes/librarian.md'] = '# Librarian reports\n\nOriginal notes are unchanged. These are AI suggestions with checked literal citations, not verified conclusions.\n\n' + '\n'.join(report_links) + '\n'
    managed, conflicts, written = state['managed'], [], 0
    # Check ownership before touching any existing file. Unknown or manually changed files survive.
    for relative,text in desired.items():
        path = safe_file(vault,relative)
        wanted = text.encode('utf-8')
        if path.exists():
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual != managed.get(relative) and actual != hashlib.sha256(wanted).hexdigest():
                conflicts.append(relative); continue
        if not path.exists() or path.read_bytes() != wanted:
            atomic(path,wanted); written += 1
        managed[relative] = hashlib.sha256(wanted).hexdigest()
    # Retired generated files stay recoverable instead of being deleted.
    retired = [name for name in managed if name not in desired and name != 'Home.md']
    for relative in retired:
        path = safe_file(vault,relative)
        if path.exists():
            if hashlib.sha256(path.read_bytes()).hexdigest() != managed[relative]:
                conflicts.append(relative)
                continue
            retired_path = safe_file(vault, 'Archive/Retired/' + managed[relative][:16] + '-' + path.name)
            retired_path.parent.mkdir(parents=True, exist_ok=True)
            if retired_path.exists() and retired_path.read_bytes() != path.read_bytes():
                conflicts.append(relative)
                continue
            os.replace(path, retired_path)
        managed.pop(relative, None)
    home = '# unanimis\n\nYour shared knowledge and personal notes.\n\n## Start here\n\n- [[Notes/README|Add a personal note]] in `Notes/`, then run `unim wiki sync`.\n- Search with `unim recall "your question"`.\n- Ask an agent to propose edits to library records.\n\n## Knowledge\n\n' + '\n'.join('- [[Indexes/' + slug(k) + '|' + k + ']] — ' + str(len(v)) for k,v in sorted(categories.items())) + '\n\n## Review\n\n' + ('\n'.join('- [[Review/' + p['id'] + '|' + p['payload']['title'].replace('|',' ') + ']]' for p in proposals) or 'No pending editorial proposals.') + '\n\n## Status\n\n' + str(len(records)) + ' stored records; ' + str(sum(r['kind']=='archive' for r in records)) + ' archived raw captures excluded from default recall.\n\n' + ('Manual edits preserved; resolve these view conflicts:\n' + '\n'.join('- '+x for x in conflicts) if conflicts else 'Generated view is current.') + '\n\nLast sync: ' + str(state.get('last_sync_at','not yet run')) + '.\n' + ('\nSync issues:\n' + '\n'.join('- '+e['path']+': '+e['error'] for e in state.get('sync_errors',[])) if state.get('sync_errors') else '') + '\n\nSource claims are not independently verified by import. `Library/` is generated; your editable notes belong in `Notes/`. No task leases or automatic approval are implied.\n'
    if librarian_reports:
        home = home.replace('## Review\n\n', '## Review\n\n- [[Indexes/librarian|Librarian reports]] — summaries, classifications, connections and cleanup suggestions.\n\n')
    path = safe_file(vault,'Home.md'); wanted=home.encode('utf-8')
    if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() not in (managed.get('Home.md'),hashlib.sha256(wanted).hexdigest()):
        conflicts.append('Home.md')
    else:
        atomic(path,wanted); managed['Home.md']=hashlib.sha256(wanted).hexdigest()
    state.update(managed=managed, conflicts=conflicts, refreshed_at=now())
    atomic(config_path(core),json.dumps(state,indent=2))
    return dict(enabled=True,vault=str(vault),written=written,conflicts=conflicts,records=len(records))


def sync(core):
    """Capture user-owned Notes and forward legacy inbox drops without an LLM."""
    with locked(core):
        state = config(core); vault=Path(state['vault']); legacy=Path(state['legacy_source'])
        saved, errors = [], []
        core._view_suspended=True
        try:
            for p in sorted((vault/'Notes').rglob('*.md')):
                if p == vault/'Notes/README.md' or any(part.startswith('.') for part in p.relative_to(vault).parts): continue
                relative=p.relative_to(vault).as_posix(); p=safe_file(vault,relative)
                raw=p.read_bytes(); fingerprint=hashlib.sha256(raw).hexdigest(); text=raw.decode('utf-8')
                previous=state['notes'].get(relative)
                if previous and previous['hash']==fingerprint: continue
                if previous:
                    current=core.get(previous['record_id'])
                    if current['revision'] != previous['revision']:
                        recoverable = current['revision'] == previous['revision'] + 1 and digest(current['content']) == fingerprint and current['provenance'].get('reason') == 'User-controlled Notes file changed: '+relative
                        if not recoverable:
                            errors.append(dict(path=relative,error='Record changed through another client; reconcile before syncing this file.')); continue
                    result=core.correct(previous['record_id'],previous['revision'],text,'User-controlled Notes file changed: '+relative,'wiki-note-edit:'+digest(str(p))+':r'+str(previous['revision'])+':'+fingerprint)
                else:
                    result=core.store(text,'wiki-note:'+digest(str(p))+':'+fingerprint,source=str(p),metadata={'human_note':True,'original_path':str(p)})
                if result.get('export_warning'):
                    errors.append(dict(path=relative,error=result['export_warning'])); continue
                state['notes'][relative]=dict(record_id=result['record_id'],revision=result['revision'],hash=fingerprint)
                saved.append(result)
                atomic(config_path(core),json.dumps(state,indent=2))
            for p in sorted((legacy/'inbox').glob('*.md')):
                if p.name=='README.md': continue
                p=safe_file(legacy,p.relative_to(legacy).as_posix())
                raw=p.read_bytes(); fingerprint=hashlib.sha256(raw).hexdigest()
                if state['inbox'].get(str(p))==fingerprint: continue
                result=core.store(raw.decode('utf-8'),'legacy-inbox:'+digest(str(p))+':'+fingerprint,source=str(p),metadata={'original_path':str(p),'legacy_inbox':True})
                if result.get('export_warning'):
                    errors.append(dict(path=str(p),error=result['export_warning'])); continue
                state['inbox'][str(p)]=fingerprint; saved.append(result)
                atomic(config_path(core),json.dumps(state,indent=2))
        finally:
            core._view_suspended=False
        state['sync_errors']=errors; state['last_sync_at']=now()
        atomic(config_path(core),json.dumps(state,indent=2))
        view=refresh(core,already_locked=True)
        return dict(saved=saved,errors=errors,view=view,mode='deterministic capture; no model calls or editorial approvals')
