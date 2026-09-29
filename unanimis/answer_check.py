"""Optional answer check for recall: does any of the top records contain what the query asks for?

Off unless UNANIMIS_ANSWER_CHECK_URL names a local TypeSafe-compatible decision endpoint (POST /v1/systemone), for
example `python -m kev.serve --run jaredpalmer/kev-9b --port 8019` from github.com/jaredpalmer/kev. Record text is
sent only to a loopback address. Each of the first TOP records is asked one yes/no question and the check reports the
highest p(yes). It is advisory, a model estimate rather than verification, and a failure never blocks recall.
"""
import json
import os
import time
import urllib.request
from urllib.parse import urlsplit

QUESTION = 'Search query: {query}\nDoes this document contain the specific information the query asks for?'
DOC_CHARS = 4000
LOOPBACK = {'127.0.0.1', 'localhost', '::1'}
LIMITATION = ('Advisory model estimate that at least one checked record contains the specific information asked for; '
              'not verification. A record that answers a yes/no question with "no" can score low. Read the records.')


def settings(env=None):
    env = os.environ if env is None else env
    if not env.get('UNANIMIS_ANSWER_CHECK_URL'):
        return None
    return dict(url=env['UNANIMIS_ANSWER_CHECK_URL'].rstrip('/'),
                threshold=float(env.get('UNANIMIS_ANSWER_CHECK_THRESHOLD', '0.5')),
                top=int(env.get('UNANIMIS_ANSWER_CHECK_TOP', '5')),
                timeout=float(env.get('UNANIMIS_ANSWER_CHECK_TIMEOUT', '10')))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


# Connect directly: ignore proxy variables and refuse redirects so record text stays on the loopback host.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def ask(url, state, instructions, timeout):
    """-> p(yes) for one yes/no ("noul") question about `state`."""
    body = json.dumps({'state': state, 'questions': {'answer': {'type': 'noul', 'instructions': instructions}}}).encode()
    request = urllib.request.Request(url + '/v1/systemone', data=body, headers={'Content-Type': 'application/json'})
    with _OPENER.open(request, timeout=timeout) as response:
        p = json.load(response)['answers']['answer']['noul']
    if type(p) not in (int, float) or not 0 <= p <= 1:
        raise ValueError('probability out of range')
    return float(p)


def check(core, query, matches, config=None, post=None):
    """-> answer_check dict for a recall result, or None when the check is not configured."""
    config = settings() if config is None else config
    if config is None:
        return None
    post = ask if post is None else post
    base = dict(threshold=config['threshold'], limitation=LIMITATION)
    if urlsplit(config['url']).hostname not in LOOPBACK:
        return dict(status='unavailable', reason='refused: UNANIMIS_ANSWER_CHECK_URL is not a loopback address', **base)
    if not matches:
        return dict(status='not_checked', reason='no matching records', **base)
    started, checked = time.monotonic(), []
    try:
        for match in matches[:config['top']]:
            record = core.get(match['record_id'])
            remaining = config['timeout'] - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError('answer check exceeded %ss' % config['timeout'])
            p = post(config['url'], (record['title'] + '\n' + record['content'])[:DOC_CHARS], QUESTION.format(query=query), remaining)
            checked.append(dict(record_id=record['record_id'], revision=record['revision'], probability=round(p, 4)))
    except Exception as error:  # the check is optional: report it, never fail recall
        return dict(status='unavailable', reason=(type(error).__name__ + ': ' + str(error))[:300], **base)
    best = max(checked, key=lambda c: c['probability'])
    return dict(status='answer_likely' if best['probability'] >= config['threshold'] else 'answer_unlikely',
                probability=best['probability'], best_record_id=best['record_id'], checked=checked,
                seconds=round(time.monotonic() - started, 2), **base)
