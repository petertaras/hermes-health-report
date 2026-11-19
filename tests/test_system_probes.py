import unittest
from tempfile import TemporaryDirectory
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import probes


class SystemProbesTest(unittest.TestCase):
    def test_disk_reports_decimal_gb(self):
        with patch.object(probes.shutil, 'disk_usage', return_value=SimpleNamespace(total=100_000_000_000, used=60_000_000_000, free=40_000_000_000)):
            row = probes._disk({'disk_mounts': ['/']})[0]
        self.assertIn('40.0 GB free', row['notes'])

    def test_disk_sizes_use_only_configured_paths_and_preserve_labels(self):
        with patch.object(probes.Path, 'is_dir', return_value=True), patch.object(probes, '_run', return_value=(0, '2500000000\t/path')) as run:
            row = next(r for r in probes._disk({'disk_mounts': [], 'disk_size_paths': ['~/example', '/other']}) if r['item'] == 'Disk usage details')
        self.assertEqual(row['sizes'], {'~/example': 2.5, '/other': 2.5})
        self.assertIn(['du', '-sb', '--', str(Path('~/example').expanduser())], [c.args[0] for c in run.call_args_list])

    def test_disk_sizes_ignore_missing_or_invalid_configuration(self):
        for value in (None, 'not-a-list', 42, [None, 42, {}]):
            with self.subTest(value=value), patch.object(probes, '_run', return_value=(1, '')) as run:
                row = next(r for r in probes._disk({'disk_mounts': [], 'disk_size_paths': value}) if r['item'] == 'Disk usage details')
                self.assertEqual(row['sizes'], {})
                self.assertFalse(any(c.args[0][0] == 'du' for c in run.call_args_list))
        with patch.object(probes, '_run', return_value=(1, '')) as run:
            probes._disk({'disk_mounts': []})
        self.assertFalse(any(c.args[0][0] == 'du' for c in run.call_args_list))

    def test_backup_requires_explicit_unit(self):
        with patch.object(probes, '_run') as run:
            row = probes._backup({})[0]
        self.assertEqual(row['notes'], 'not configured')
        run.assert_not_called()
        with patch.object(probes, '_run', return_value=(1, '')) as run:
            probes._backup({'backup_unit': 'custom-backup'})
        self.assertEqual(run.call_args_list[0].args[0][3], 'custom-backup.service')

    def test_plugin_names_preserve_configured_casing(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'plugins' / 'examplePlugin').mkdir(parents=True)
            state = root / 'hermes-health-report'
            state.mkdir()
            (state / 'maintenance-latest.json').write_text('{"plugin_results":[{"name":"examplePlugin","status":"failed"}]}')
            with patch.object(probes, '_plugin_checks', return_value=({}, 'offline')):
                rows = probes._plugins(root, [('main', root)])
        self.assertEqual([r['item'] for r in rows].count('examplePlugin'), 2)

    def test_chromium_compares_stable_numerically(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, size): return b'{"channels":{"Stable":{"version":"120.10.0.0"}}}'
        with patch.object(probes, '_yaml', return_value={'browser': {'executable_path': '/browser'}}), patch.object(probes, '_latest', return_value=('', 'offline')), patch.object(probes, '_run', side_effect=lambda args, **kw: (0, 'Chromium 120.9.0.0') if args[0] == '/browser' else (1, '')), patch.object(probes.urllib.request, 'urlopen', return_value=Response()):
            row = next(r for r in probes._release(Path('/missing'), Path('/missing')) if r['item'] == 'Chromium')
        self.assertEqual(row['status'], 'warn')
        self.assertIn('Chrome for Testing', row['notes'])
        self.assertIn('120.10.0.0', row['notes'])

    def test_services_discover_user_units(self):
        def run(args, **kwargs):
            if args[:3] == ['systemctl', '--user', 'show']:
                return (0, 'LoadState=loaded\nActiveState=active')
            return (1, '')
        with patch.object(probes, '_run', side_effect=run), patch.object(probes, '_latest', return_value=('', 'offline')):
            rows = probes._services({})
        self.assertEqual({r['item'] for r in rows} & {'hermes-gateway.service', 'hermes-serve.service'}, {'hermes-gateway.service', 'hermes-serve.service'})

    def test_services_explicit_units_disable_discovery(self):
        with patch.object(probes, '_run', return_value=(1, '')) as run, patch.object(probes, '_latest', return_value=('', 'offline')):
            probes._services({'services': ['example.service']})
        self.assertFalse(any(call.args[0][:3] == ['systemctl', '--user', 'show'] for call in run.call_args_list))

    def test_missing_units_are_not_reported_and_failed_unit_warns(self):
        def run(args, **kwargs):
            if args[:3] == ['systemctl', '--user', 'show']:
                return (0, 'LoadState=loaded\nActiveState=failed') if args[3] == 'hermes-gateway.service' else (0, 'LoadState=not-found\nActiveState=inactive')
            return (1, '')
        with patch.object(probes, '_run', side_effect=run), patch.object(probes, '_latest', return_value=('', 'offline')):
            rows = probes._services({})
        gateway = next(r for r in rows if r['item'] == 'hermes-gateway.service')
        self.assertEqual(gateway['status'], 'warn')
        self.assertFalse(any(r['item'] == 'hermes-serve.service' for r in rows))

    def test_stable_network_failure_is_reported_as_unavailable(self):
        with patch.object(probes, '_yaml', return_value={'browser': {'executable_path': '/browser'}}), patch.object(probes, '_latest', return_value=('', 'offline')), patch.object(probes, '_run', side_effect=lambda args, **kw: (0, 'Chromium 120.9.0.0') if args[0] == '/browser' else (1, '')), patch.object(probes.urllib.request, 'urlopen', side_effect=OSError('offline')):
            row = next(r for r in probes._release(Path('/missing'), Path('/missing')) if r['item'] == 'Chromium')
        self.assertEqual(row['status'], 'info')
        self.assertIn('comparison unavailable', row['notes'])