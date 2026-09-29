"""Verified portable snapshots; restore only into a new directory."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat
import tempfile
import zipfile
from .core import UnimError, now

MAX_BYTES=2*1024**3


def sha(data):return hashlib.sha256(data).hexdigest()


def files(core):
    roots=[('data',core.data_dir)]
    config=core.data_dir/'wiki.json'
    if config.is_file(): roots.append(('vault',Path(json.loads(config.read_text())['vault'])))
    result={}
    for prefix,root in roots:
        for path in sorted(root.rglob('*')):
            rel=path.relative_to(root)
            if prefix=='data' and (rel.parts[0] in ('models','semantic.sqlite3','semantic.sqlite3-wal','semantic.sqlite3-shm','unanimis.sqlite3','unanimis.sqlite3-wal','unanimis.sqlite3-shm')):continue
            if path.is_symlink():raise UnimError('unsafe_path','Backup refuses symlinks: '+str(path))
            if path.is_file():result[prefix+'/'+rel.as_posix()]=path
    # Snapshot only the running package, so an installed copy never sweeps up unrelated site-packages files.
    package=Path(__file__).resolve().parent;app=package.parent
    app_files=list(package.rglob('*.py'))+[package/'agent-policy.md']
    for path in app_files:
        if path.is_symlink():raise UnimError('unsafe_path','Backup refuses application symlinks')
        if path.is_file():result['app/'+path.relative_to(app).as_posix()]=path
    return result


def create(core,destination):
    if core.read_only:raise UnimError('read_only','Backup export requires an authorized administrative connection')
    dest=Path(destination).expanduser().resolve()
    if dest.exists():raise UnimError('exists','Choose a new backup filename; existing backups are never overwritten')
    for root in [core.data_dir.resolve()]+([Path(json.loads((core.data_dir/'wiki.json').read_text())['vault']).resolve()] if (core.data_dir/'wiki.json').exists() else []):
        if root==dest or root in dest.parents:raise UnimError('unsafe_path','Save the backup outside the data and vault directories')
    dest.parent.mkdir(parents=True,exist_ok=True)
    paths=files(core); fingerprints={k:sha(p.read_bytes()) for k,p in paths.items()}
    version=core.db.execute('PRAGMA data_version').fetchone()[0]
    with tempfile.TemporaryDirectory(prefix='unim-backup-') as temp:
        dbpath=Path(temp)/'unanimis.sqlite3'
        target=sqlite3.connect(dbpath);core.db.backup(target)
        if target.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise UnimError('invalid_backup','Snapshot database integrity check failed')
        counts={table:target.execute('SELECT count(*) FROM '+table).fetchone()[0] for table in ['records','revisions','proposals']}
        target.close()
        paths['data/unanimis.sqlite3']=dbpath
        manifest=dict(format='unanimis-backup-v2',created_at=now(),scope='entire database, bound vault and application',counts=counts,files={},excluded=['models','semantic index (rebuildable)'])
        archive=Path(temp)/'backup.zip'
        with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED) as z:
            for name,path in paths.items():
                data=path.read_bytes();manifest['files'][name]=dict(sha256=sha(data),bytes=len(data),mode=path.stat().st_mode & 0o777);z.writestr(name,data)
            z.writestr('manifest.json',json.dumps(manifest,indent=2))
        current=files(core)
        if set(current)!=set(fingerprints) or any(sha(p.read_bytes())!=fingerprints[k] for k,p in current.items()) or core.db.execute('PRAGMA data_version').fetchone()[0]!=version:
            raise UnimError('conflict','Sources changed during backup; no backup published. Retry during a quiet interval')
        verify(archive)
        # Exclusive creation, including races with other writers.
        with os.fdopen(os.open(dest,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'wb') as output, archive.open('rb') as source:shutil.copyfileobj(source,output)
    return dict(path=str(dest),sha256=sha(dest.read_bytes()),counts=counts,files=len(manifest['files']),verified=True)


def _verify(bundle):
    with zipfile.ZipFile(bundle) as z:
        entries=z.infolist();names=[x.filename for x in entries]
        if len(names)!=len(set(names)) or len(names)>100000:raise UnimError('invalid_backup','Duplicate or excessive archive entries')
        if sum(x.file_size for x in entries)>MAX_BYTES:raise UnimError('invalid_backup','Backup exceeds 2 GiB restore limit')
        for entry in entries:
            p=PurePosixPath(entry.filename)
            if p.is_absolute() or '..' in p.parts or '\\' in entry.filename or stat.S_ISLNK(entry.external_attr>>16):raise UnimError('invalid_backup','Unsafe archive path')
            if entry.filename!='manifest.json' and (not p.parts or p.parts[0] not in ('data','vault','app')):raise UnimError('invalid_backup','Unexpected archive root')
        manifest=json.loads(z.read('manifest.json'))
        if manifest.get('format') not in ('unanimis-backup-v1','unanimis-backup-v2') or set(manifest['files'])!=set(names)-{'manifest.json'}:raise UnimError('invalid_backup','Manifest does not match archive')
        if 'data/unanimis.sqlite3' not in manifest['files']:raise UnimError('invalid_backup','Database is missing')
        for name,info in manifest['files'].items():
            data=z.read(name)
            if len(data)!=info['bytes'] or sha(data)!=info['sha256']:raise UnimError('invalid_backup','Checksum mismatch: '+name)
    return dict(verified=True,files=len(manifest['files']),counts=manifest['counts'],format=manifest['format'])


def verify(bundle):
    try:return _verify(bundle)
    except (zipfile.BadZipFile, KeyError, ValueError, TypeError) as error:
        raise UnimError('invalid_backup','Invalid backup manifest or archive ('+type(error).__name__+')') from error


def restore(bundle,destination):
    verified=verify(bundle)
    dest=Path(destination).expanduser().absolute()
    if dest.exists() or dest.is_symlink():raise UnimError('exists','Restore requires a new directory; original files are never overwritten')
    dest.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.unim-restore-',dir=dest.parent) as temp:
        staging=Path(temp)/'restored';staging.mkdir()
        with zipfile.ZipFile(bundle) as z:
            manifest=json.loads(z.read('manifest.json'))
            for name in z.namelist():
                if name=='manifest.json':continue
                p=staging/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(z.read(name));p.chmod(manifest['files'][name].get('mode',0o644) & 0o777)
        db=sqlite3.connect(staging/'data/unanimis.sqlite3')
        if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or list(db.execute('PRAGMA foreign_key_check')):raise UnimError('invalid_backup','Restored database validation failed')
        counts={t:db.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ['records','revisions','proposals']};db.close()
        if counts!=verified['counts']:raise UnimError('invalid_backup','Database counts do not match the manifest')
        config=staging/'data/wiki.json'
        if config.exists():
            value=json.loads(config.read_text());value['vault']=str(dest/'vault')
            # Prevent a restored test instance from reading the original legacy inbox.
            value['legacy_source']=str(dest/'legacy-disconnected')
            config.write_text(json.dumps(value,indent=2))
        if (staging/'app/unanimis/__main__.py').exists():
            launcher = "#!/usr/bin/env python3\nimport os,sys\nfrom pathlib import Path\nroot=Path(__file__).resolve().parent\nenv=dict(os.environ,PYTHONPATH=str(root/'app'))\nos.execve(sys.executable,[sys.executable,'-m','unanimis','--data-dir',str(root/'data'),*sys.argv[1:]],env)\n"
            (staging/'unim').write_text(launcher);(staging/'unim').chmod(0o755)
        # mkdir reserves the destination exclusively; moving files cannot follow user symlinks.
        dest.mkdir(mode=0o700)
        try:
            for item in staging.iterdir():shutil.move(str(item),str(dest/item.name))
        except BaseException:
            # Preserve any partial restore for inspection, never delete user data.
            raise UnimError('restore_incomplete','Restore interrupted; inspect the new destination before retrying')
    return dict(restored=str(dest),data_dir=str(dest/'data'),vault=str(dest/'vault'),launcher=str(dest/'unim') if (dest/'unim').exists() else None,counts=counts,legacy_inbox='disconnected',semantic_index='rebuild with unim --data-dir PATH search index')
