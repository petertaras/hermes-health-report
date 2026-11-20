import io
import sys
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import probes
from rendering import render


class LookupDiagnosticsTests(unittest.TestCase):
    def test_hermes_release_uses_scoped_github_token(self):
        scope = SimpleNamespace(get_secret=Mock(return_value='scoped-fixture'), UnscopedSecretError=RuntimeError)
        with patch.dict(sys.modules, {'agent.secret_scope': scope}), \
                patch.dict(probes.os.environ, {'GITHUB_TOKEN': 'unscoped-fixture'}), \
                patch.object(probes, '_run', return_value=(1, '')) as run, \
                patch.object(probes, '_chromium_stable', return_value=''), \
                patch.object(probes.urllib.request.OpenerDirector, 'open', return_value=io.BytesIO(b'{"tag_name":"v1.2.3"}')) as open_url:
            row = probes._release(Path('/missing'), Path('/missing'))[0]
        self.assertIn('upstream: v1.2.3 (via authenticated GitHub API)', row['notes'])
        self.assertEqual(open_url.call_args.args[0].get_header('Authorization'), 'Bearer scoped-fixture')
        self.assertTrue(scope.get_secret.call_args_list)
        self.assertTrue(all(call.args == ('GITHUB_TOKEN',) for call in scope.get_secret.call_args_list))
        self.assertFalse(any(call.args[0][0] == 'gh' for call in run.call_args_list))

    def test_tailscale_release_reports_anonymous_rate_limit(self):
        headers = Message()
        headers['X-RateLimit-Remaining'] = '0'
        error = HTTPError('https://api.github.com', 403, 'Forbidden', headers, None)
        with patch.dict(probes.os.environ, {}, clear=True), patch.object(probes, '_run', return_value=(127, '')), patch.object(probes.urllib.request, 'urlopen', side_effect=error):
            row = probes._services({})[-1]
        self.assertIn('unauthenticated GitHub API rate limit reached', row['notes'])

    def test_release_label_requires_an_exact_tag(self):
        for tags, expected in (('v1.2.3\nother-tag', 'Latest release'),
                               ('v1.2.2', 'Stable release'), ('', '')):
            def run(argv, **kwargs):
                if argv[0] != 'git':
                    return 127, ''
                if argv[3] == 'tag':
                    return 0, tags
                if argv[3] == 'describe':
                    return 0, 'other-tag' if tags else 'v1.2.3'
                return 0, 'abc'

            with self.subTest(tags=tags), patch.object(probes, '_run', side_effect=run), \
                    patch.object(probes, '_latest', return_value=('v1.2.3', '', '')), \
                    patch.object(probes, '_chromium_stable', return_value=''):
                rows = probes._release(Path('/missing'), Path('/missing'))
                self.assertEqual(rows[0]['release'], expected)
                self.assertEqual(rows[0]['version'], 'v1.2.2' if tags == 'v1.2.2' else 'v1.2.3')
                page = render('i', 'h', {'Hermes Agent release': rows})
                self.assertIn('href="https://github.com/NousResearch/hermes-agent/releases" style="color:#0969da">Latest release</a>', page)
                self.assertIn('href="https://github.com/NousResearch/hermes-agent/releases/tag/v1.2.3"', page)
                if expected:
                    self.assertIn(f' · {expected} (abc)', page)
                else:
                    self.assertNotIn('is on the latest release', page)

    def test_plugin_notes_use_dash_only_for_no_update(self):
        self.assertEqual(probes._plugin_check_note({'update_available': False}, ''), ('ok', '–'))
        self.assertIn('pinned', probes._plugin_check_note({'update_available': False, 'reason': 'pinned'}, '')[1])

    def test_plugin_worker_timeout_keeps_results_and_explains_missing_ones(self):
        timeout = probes.subprocess.TimeoutExpired('worker', 15, output=b'{"name":"first","class":"manual","reason":"no provenance; not auto-updatable"}\n')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ('first', 'second'):
                (root / 'plugins' / name).mkdir(parents=True)
            with patch.object(probes.subprocess, 'run', side_effect=timeout):
                rows = {r['item']: r for r in probes._plugins(root, [('main', root)])}
        self.assertIn('no recorded install source', rows['first']['notes'])
        self.assertIn('timed out', rows['second']['notes'])

    def test_plugin_notes_do_not_repeat_sensitive_details(self):
        result = SimpleNamespace(returncode=0, stdout=(
            '{"name":"feed","reason":"feed fetch failed: https://host/secret"}\n'
            '{"name":"drift","needs_fixing":"manifest declares https://host/secret"}\n'
        ))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ('feed', 'drift'):
                (root / 'plugins' / name).mkdir(parents=True)
            with patch.object(probes.subprocess, 'run', return_value=result):
                rows = {r['item']: r for r in probes._plugins(root, [('main', root)])}
        self.assertIn('update feed could not be fetched', rows['feed']['notes'])
        self.assertIn('not trusted', rows['drift']['notes'])
        self.assertNotIn('secret', str(rows))
