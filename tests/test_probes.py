import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import probes
from rendering import render


class ProbeTests(unittest.TestCase):
    def test_partial_report_and_profile_vault_privacy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / '.hermes'
            profile = root / 'profiles' / 'work'
            (profile / 'memories').mkdir(parents=True)
            (profile / 'memories' / 'MEMORY.md').write_text('abcd')
            (profile / 'config.yaml').write_text('memory:\n  memory_char_limit: 8\n')
            vault = Path(tmp) / 'private-vault'
            vault.mkdir()
            (vault / 'secret.md').write_text('not to be read')
            with patch('probes._run', return_value=(127, 'unavailable')):
                result = probes.collect(profile, {'vaults': [{'name': 'notes', 'path': str(vault)}], 'disk_mounts': []})
            self.assertEqual(list(result), probes.SECTIONS)
            self.assertTrue(all(isinstance(rows, list) and rows for rows in result.values()))
            rows = result['Memory']
            memory = next(x for x in rows if x.get('profile') == 'work' and x['item'] == 'MEMORY.md')
            self.assertIn('50', memory['notes'])
            vaultrow = next(x for x in rows if x['item'] == 'notes')
            self.assertEqual(vaultrow['vault_path'], str(vault))
            self.assertNotIn(str(vault), str({k: v for k, v in vaultrow.items() if k != 'vault_path'}))
            for section in result.values():
                for row in section:
                    self.assertIn(row['status'], ('ok', 'info', 'warn', 'fail'))
                    self.assertTrue(all(isinstance(row[key], str) for key in ('status', 'item', 'version', 'notes')))

    def test_configured_browser_override_does_not_fall_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / '.herermes'
            root.mkdir()
            (root / 'config.yaml').write_text('browser:\n  executable_path: /missing/browser\n')
            with patch('probes._run', return_value=(127, 'unavailable')):
                rows = probes.collect(root, {'disk_mounts': []})['Hermes Agent release']
            browser = next(row for row in rows if row['item'] == 'Chromium')
            self.assertEqual(browser['status'], 'warn')
            self.assertIn('configured', browser['notes'].lower())

    def test_maintenance_results_and_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / 'hermes-health-report'
            state.mkdir(parents=True)
            (state / 'maintenance-latest.json').write_text('{"plugin_results":[{"name":"example","status":"failed","home":"work"}],"restart_required":true}')
            rows = probes._plugins(root, [('main', root)])
            self.assertEqual(next(r for r in rows if r['item'] == 'example')['status'], 'fail')
            self.assertEqual(next(r for r in rows if r['item'] == 'Maintenance restart')['status'], 'warn')

    def test_tailscale_update_available(self):
        with patch('probes._run', return_value=(0, '1.2.3')), patch('probes._latest', return_value=('v1.3.0', 'via GitHub CLI')):
            row = probes._services({})[-1]
            self.assertEqual(row['status'], 'warn')
            self.assertIn('update available', row['notes'])

    def test_argv_probe_failure_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp, patch('probes._run', return_value=(1, 'failed')):
            rows = probes.collect(Path(tmp), {'disk_mounts': [], 'probes': [{'name': 'check', 'argv': ['false']}]})['Services and other dependencies']
            row = next(row for row in rows if row['item'] == 'check')
            self.assertEqual(row['status'], 'warn')

    def test_dashboard_link_comes_from_safe_config_url(self):
        section = 'Services and other dependencies'
        url = 'https://dashboard.example.test:8443/console'
        with patch('probes._run', return_value=(127, '')), patch('probes._latest', return_value=('', '')):
            page = render('i', 'h', {section: probes._services({'dashboard_url': url})})
            self.assertIn('>Hermes dashboard</td>', page)
            self.assertIn(f'href="{url}"', page)
            for invalid in ('', 'javascript:alert(1)', 'https://user:secret@example.test'):
                with self.subTest(invalid=invalid):
                    page = render('i', 'h', {section: probes._services({'dashboard_url': invalid})})
                    self.assertNotIn('Hermes dashboard', page)
                    self.assertNotIn('secret', page)


if __name__ == '__main__':
    unittest.main()
