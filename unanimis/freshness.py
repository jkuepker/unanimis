"""Read-time revision-reference checks. No model calls, network, or writes."""
import json
from datetime import datetime, timezone

VERSION = 'librarian-v1'
MAX_REFERENCES = 64

def metadata(record, core=None):
    """Resolve librarian identity without rewriting immutable provenance.

    Only revisions at or before the requested revision participate. Edited
    reports supply their current body to future drafts, never an old summary.
    Acceptance remains a property of the requested revision.
    """
    current = record['provenance'].get('metadata', {})
    if current.get('librarian_version') == VERSION or core is None:
        return current
    history = list(core.db.execute(
        'SELECT revision,provenance FROM revisions WHERE record_id=? AND revision<? ORDER BY revision DESC',
        (record['record_id'], record['revision'])))
    for ancestor in history:
        original = json.loads(ancestor['provenance']).get('metadata', {})
        if original.get('librarian_version') != VERSION:
            continue
        result = dict(original)
        evidence = list(original['analysis']['evidence'])
        # Keep original dependencies: an editorial citation is not proof that
        # every older claim was revalidated. New dependencies also participate.
        for row in reversed(history):
            if row['revision'] > ancestor['revision']:
                evidence.extend(json.loads(row['provenance']).get('evidence', []))
        evidence.extend(record['provenance'].get('evidence', []))
        unique = {}
        for cite in evidence:
            if cite['record_id'] != record['record_id']:
                unique[(cite['record_id'], cite['revision'], cite['quote'])] = cite
        result['analysis'] = dict(report_content=record['content'], evidence=list(unique.values()))
        result['status'] = record['acceptance']
        result['metadata_revision'] = ancestor['revision']
        return result
    return current


def _assess(core, record):
    """Check direct references only; matching versions do not verify claims."""
    result = dict(status='not_assessed', checked_at=datetime.now(timezone.utc).isoformat(),
                  basis='direct_record_revision_references', references=[], checks_truncated=False,
                  limitation='Checks direct cited revisions only, not downstream evidence, semantic truth, or live operational state.')
    context = metadata(record, core)
    is_report = context.get('librarian_version') == VERSION
    evidence = list(context.get('analysis', {}).get('evidence', [])) if is_report else list(record['provenance'].get('evidence', []))
    if is_report:
        target = context.get('target', {})
        evidence.append(dict(record_id=target.get('record_id'), revision=target.get('revision')))
    references = {}
    malformed = False
    for cite in evidence:
        if not isinstance(cite, dict):
            malformed = True
            continue
        rid, revision = cite.get('record_id'), cite.get('revision')
        if not isinstance(rid, str) or not rid or type(revision) is not int or revision < 1:
            malformed = True
            continue
        if rid == record['record_id']:
            continue
        references[(rid, revision)] = None
    if not references and not malformed:
        return result
    result['checks_truncated'] = len(references) > MAX_REFERENCES
    unavailable = malformed or result['checks_truncated']
    stale = False
    for rid, revision in list(references)[:MAX_REFERENCES]:
        # Scope before returning any current revision. Do not recursively call get.
        row = core.db.execute('SELECT current_revision FROM records WHERE id=? AND project=? AND scope=?',
                              (rid, core.project, core.scope)).fetchone()
        exists = row is not None and core.db.execute('SELECT 1 FROM revisions WHERE record_id=? AND revision=?', (rid, revision)).fetchone() is not None
        current = row['current_revision'] if row is not None else None
        state = 'unavailable' if not exists else 'stale' if current != revision else 'references_current'
        unavailable = unavailable or state == 'unavailable'
        stale = stale or state == 'stale'
        result['references'].append(dict(record_id=rid, cited_revision=revision, current_revision=current, status=state))
    result['status'] = 'stale' if stale else 'unavailable' if unavailable else 'references_current'
    if malformed:
        result['reason'] = 'Some revision references are malformed.'
    if unavailable:
        result['incomplete'] = True
    return result


def assess(core, record):
    try:
        return _assess(core, record)
    except (KeyError, TypeError, AttributeError):
        return dict(status='unavailable', checked_at=datetime.now(timezone.utc).isoformat(),
                    basis='direct_record_revision_references', references=[], checks_truncated=False,
                    incomplete=True, reason='Revision-reference metadata is malformed.',
                    limitation='Claims and live operational state are not independently verified.')
