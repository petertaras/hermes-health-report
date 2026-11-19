import unittest
from pathlib import Path
from unittest.mock import patch

import probes


class BrowserImagesTest(unittest.TestCase):
    def test_terminal_images_are_deduplicated_and_compared(self):
        root = Path('/nonexistent-test-root')
        image = 'ghcr.io/example/playwright:v1.40.0-cft120'
        profiles = [('main', root), ('one', root / 'one'), ('two', root / 'two')]
        configs = {
            root / 'config.yaml': {'terminal': {'docker_image': image}},
            root / 'one/config.yaml': {'terminal': {'docker_image': image}},
            root / 'two/config.yaml': {'terminal': {'docker_image': 'example/custom:latest'}},
        }
        def run(args, **kwargs):
            if args[:3] == ['docker', 'image', 'inspect']:
                return (0, '2024-01-02T03:04:05Z')
            return (1, '')
        with patch.object(probes, '_profiles', return_value=profiles), \
             patch.object(probes, '_yaml', side_effect=lambda path: configs.get(path, {})), \
             patch.object(probes, '_run', side_effect=run) as command, \
             patch.object(probes, '_chromium_stable', return_value='123.0.0.0'), \
             patch.object(probes, '_latest', return_value=('v1.50.0', 'test')), \
             patch.object(Path, 'home', return_value=root):
            rows = probes._release(root, root)
        images = [row for row in rows if row['item'].startswith('Sandbox image ')]
        self.assertEqual(len(images), 2)
        row = next(row for row in images if image in row['item'])
        self.assertIn('Playwright 1.40.0', row['version'])
        self.assertIn('Chrome 120', row['version'])
        self.assertIn('2024-01-02', row['notes'])
        self.assertIn('3 Chrome versions behind', row['notes'])
        self.assertIn('v1.50.0', row['notes'])
        self.assertEqual(row['status'], 'warn')
        inspect = [call.args[0] for call in command.call_args_list if call.args[0][:3] == ['docker', 'image', 'inspect']]
        self.assertEqual(len(inspect), 2)
        self.assertTrue(all(args[3:6] == ['--format', '{{.Created}}', '--'] for args in inspect))

    def test_inspection_failure_and_unavailable_versions_are_reported(self):
        root = Path('/nonexistent-test-root')
        image = 'registry:5000/team/playwright:v1.40.0-cft120'
        with patch.object(probes, '_profiles', return_value=[('main', root)]), \
             patch.object(probes, '_yaml', return_value={'terminal': {'docker_image': image}}), \
             patch.object(probes, '_run', side_effect=lambda args, **kw: (127, '') if args[0] == 'docker' else (1, '')), \
             patch.object(probes, '_chromium_stable', return_value=''), \
             patch.object(probes, '_latest', return_value=('', 'offline')), \
             patch.object(Path, 'home', return_value=root):
            row = next(r for r in probes._release(root, root) if r['item'].startswith('Sandbox image '))
        self.assertIn(image, row['item'])
        self.assertIn('Docker not available', row['notes'])
        self.assertIn('Playwright 1.40.0', row['version'])
        self.assertNotIn('behind', row['notes'])

    def test_no_latest_note_for_current_or_newer_playwright(self):
        root = Path('/nonexistent-test-root')
        with patch.object(probes, '_profiles', return_value=[('main', root)]), \
             patch.object(probes, '_yaml', return_value={'terminal': {'docker_image': 'playwright:v1.50.0-cft123'}}), \
             patch.object(probes, '_run', return_value=(1, '')), \
             patch.object(probes, '_chromium_stable', return_value='123.0.0.0'), \
             patch.object(probes, '_latest', return_value=('v1.50.0', 'test')), \
             patch.object(Path, 'home', return_value=root):
            row = next(r for r in probes._release(root, root) if r['item'].startswith('Sandbox image '))
        self.assertNotIn('Playwright latest', row['notes'])
        self.assertIn('inspect unavailable', row['notes'])
        self.assertEqual(row['status'], 'info')
