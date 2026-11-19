import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import probes
import run


class MemoryProbeTests(unittest.TestCase):
    def test_email_preview_includes_vault_path_and_configured_hindsight(self):
        health = run.load()
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            vault = home / 'example-vault'
            vault.mkdir()
            (vault / 'document.md').touch()
            config = {'vaults': [{'path': str(vault)}], 'hindsight': {
                'web_url': 'https://console.example.test', 'api_url': 'https://api.example.test/base',
            }}
            response = MagicMock(status=200)
            response.__enter__.return_value = response
            response.read.return_value = b'{"status":"healthy"}'
            with patch('urllib.request.urlopen', return_value=response) as request:
                rows = probes._memory([], config)
            self.assertEqual(request.call_args.args[0].full_url, 'https://api.example.test/base/health')
            with patch.object(health.probes, 'collect', return_value={'Memory': rows}), \
                    patch.object(health.delivery, 'Graph') as graph:
                private = Path(health.report(home, config, 'health', True, preview_email=True)).read_text()
                default = Path(health.report(home, config, 'health', True)).read_text()
                with self.assertRaisesRegex(ValueError, 'requires --dry-run'):
                    health.report(home, config, 'health', preview_email=True)
                graph.assert_not_called()
            self.assertIn(f'>{vault}</span>', private)
            self.assertIn('>1</td>', private.split('<h3>Vaults</h3>', 1)[1])
            self.assertNotIn(str(vault), default)
            self.assertIn('href="https://console.example.test"', private)
            self.assertIn('healthy', private)
            self.assertNotIn('api.example.test', private)

    def test_hindsight_rejects_unsafe_report_urls(self):
        with patch('urllib.request.urlopen') as request:
            rows = probes._memory([], {'hindsight': {
                'web_url': 'javascript:alert(1)', 'api_url': 'https://user:secret@example.test',
            }})
        request.assert_not_called()
        self.assertEqual(rows[0]['hindsight_web'], '')
        self.assertIn('not configured', rows[0]['notes'])
        self.assertNotIn('secret', str(rows))

    def test_memory_excludes_archive_and_main_aliases(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ('.deleted', 'default', 'main', 'work'):
                (root / 'profiles' / name).mkdir(parents=True)
            rows = probes._memory(probes._profiles(root), {})
        self.assertEqual([r['profile'] for r in rows if r['item'] == 'MEMORY.md'], ['main', 'work'])

    def test_hindsight_health_cached_and_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            homes = [Path(tmp) / 'a', Path(tmp) / 'b']
            for home in homes:
                (home / 'hindsight').mkdir(parents=True)
                (home / 'config.yaml').write_text('memory:\n  provider: hindsight\n')
                (home / 'hindsight' / 'config.json').write_text(json.dumps({'api_url': 'http://localhost:1234/private'}))
            class Response:
                status = 200
                def __enter__(self): return self
                def __exit__(self, *args): pass
                def read(self, n): return b'{"status":"ok", "secret":"private"}'
            with patch('urllib.request.urlopen', return_value=Response()) as request:
                rows = probes._memory([(h.name, h) for h in homes], {})
            providers = [r for r in rows if r['item'] == 'Memory provider']
            self.assertEqual([r['status'] for r in providers], ['ok', 'ok'])
            self.assertEqual(request.call_count, 1)
            self.assertEqual(request.call_args.args[0].full_url, 'http://localhost:1234/private/health')
            self.assertNotIn('localhost', str(rows))
            self.assertNotIn('secret', str(rows))

    def test_hindsight_unavailable_is_warning_without_error_details(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / 'hindsight').mkdir()
            (home / 'config.yaml').write_text('memory:\n  provider: hindsight\n')
            (home / 'hindsight' / 'config.json').write_text(json.dumps({'api_url': 'http://localhost:1234'}))
            with patch('urllib.request.urlopen', side_effect=OSError('secret')):
                row = next(r for r in probes._memory([('work', home)], {}) if r['item'] == 'Memory provider')
            self.assertEqual(row['status'], 'warn')
            self.assertNotIn('secret', str(row))

    def test_hindsight_invalid_port_does_not_abort_collection(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / 'hindsight').mkdir()
            (home / 'hindsight' / 'config.json').write_text('{"api_url":"http://localhost:bad"}')
            self.assertEqual(probes._memory_health(home, {}), ('warn', 'health unavailable'))

    def test_vault_fallback_from_profile_env_and_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / 'private'
            vault.mkdir()
            note = vault / 'note.md'
            note.write_text('secret')
            os.utime(note, (1704067200, 1704067200))
            home = Path(tmp) / 'profile'
            home.mkdir()
            (home / '.env').write_text(f'OBSIDIAN_VAULT_PATH="{vault}"\n')
            try:
                with patch.dict(os.environ, {'TZ': 'AEST-10'}):
                    time.tzset()
                    rows = probes._memory([('work', home)], {})
            finally:
                time.tzset()
            row = next(r for r in rows if r.get('vault'))
            self.assertEqual(row['item'], 'private')
            self.assertEqual(row['last_change'], '2024-01-01 10:00:00')
            self.assertEqual(row['vault_path'], str(vault))
            self.assertEqual(row['documents'], 1)
            self.assertNotIn(str(vault), str({k:v for k,v in row.items() if k != 'vault_path'}))
            (home / '.env').unlink()
            rows = probes._memory([('work', home)], {})
            self.assertTrue(any(r.get('vault') and 'not configured' in r['notes'] for r in rows))
