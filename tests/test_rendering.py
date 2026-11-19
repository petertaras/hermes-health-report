import unittest

from rendering import render


class RenderingTests(unittest.TestCase):
    def test_report_renders_named_failures_and_ordered_sections(self):
        sections = {
            'Disk': [{'status': 'fail', 'item': 'NVMe', 'version': '', 'notes': 'bad'}],
            'Backup': [{'status': 'ok', 'item': 'timer', 'version': '1', 'notes': 'ready'}],
        }
        page = render('main', 'host', sections, report_id='unique-123')
        self.assertIn('Hermes health report', page)
        self.assertIn('Summary', page)
        self.assertIn('NVMe', page)
        self.assertIn('❌', page)
        self.assertLess(page.index('1. Backup'), page.index('2. Disk'))
        self.assertIn('unique-123', page)

    def test_escaping_and_email_only_fields(self):
        row = {'status': 'warn', 'item': '<script>x</script>', 'version': '"v"',
               'notes': 'a & b', 'profile': '<profile>', 'vault_path': '/private/vault'}
        public = render('<instance>', '<host>', {'Plugins': [row]}, actions=['<restart>'], report_id='<id>')
        self.assertNotIn('<script>', public)
        self.assertIn('&lt;script&gt;', public)
        self.assertIn('&lt;instance&gt;', public)
        self.assertIn('&lt;host&gt;', public)
        self.assertIn('&lt;restart&gt;', public)
        self.assertIn('&lt;id&gt;', public)
        self.assertIn('&quot;v&quot;', public)
        self.assertIn('a &amp; b', public)
        self.assertIn('&lt;profile&gt;', public)
        self.assertNotIn('/private/vault', public)
        mail = render('i', 'h', {'Plugins': [row]}, email=True)
        self.assertIn('&lt;profile&gt;', mail)
        self.assertIn('/private/vault', mail)

    def test_plugins_group_by_name_and_installed_version(self):
        def plugin(profile, version, state=None):
            return dict(status='fail' if state == 'failed' else 'ok', item='widget',
                        version=version, notes=state or '–', profile=profile,
                        maintenance_status=state)

        page = render('i', 'h', {'Plugins': [plugin('alpha', '1'), plugin('beta', '1'),
                                            plugin('gamma', '2'), plugin('gamma', '2', 'updated'),
                                            plugin('alpha', '1', 'failed'), plugin('beta', '1', 'skipped')]})
        self.assertEqual(page.count('<span style="font-weight:600">widget</span><br>'), 2)
        self.assertIn('>–', page)
        self.assertIn('alpha, beta', page)
        self.assertIn('gamma', page)
        self.assertEqual(page.count('1 updated, 1 failed, 1 not updated.'), 2)

    def test_memory_renders_one_row_per_profile_and_named_vaults(self):
        def fact(item, profile, notes, version=''):
            return dict(status='info', item=item, profile=profile, notes=notes, version=version)

        rows = [fact('MEMORY.md', 'alpha', 'on; 50% used'), fact('USER.md', 'alpha', 'off'),
                dict(fact('Memory provider', 'alpha', 'healthy', 'hindsight'), status='ok'),
                fact('MEMORY.md', 'beta', 'on; file unavailable'),
                fact('Memory provider', 'beta', 'health not available'),
                dict(status='info', item='Notes A', notes='', version='', documents=2,
                     last_change='2024-01-01 10:00:00', vault=True, vault_path='/private/notes-a'),
                dict(status='warn', item='Notes B', notes='vault unavailable', version='', vault=True)]
        page = render('i', 'h', {'Memory': rows})
        table = page.split('6. Memory</h2>', 1)[1].split('</table>', 1)[0]
        for label in ('Profile', 'MEMORY.md', 'USER.md', 'Provider'):
            self.assertIn(f'>{label}</th>', table)
        self.assertEqual(table.count('<tr>'), 3)
        self.assertIn('>✅ hindsight</td>', table)
        self.assertIn('>–</td>', table)
        self.assertNotIn('healthy', table)
        self.assertIn('Notes A', page)
        self.assertIn('Notes B', page)
        self.assertNotIn('/private/notes-a', page)
        vaults = render('i', 'h', {'Memory': rows}, email=True).split('<h3>Vaults</h3>', 1)[1].split('</table>', 1)[0]
        for heading in ('Name', 'Documents', 'Last change'):
            self.assertIn(f'>{heading}</th>', vaults)
        self.assertIn('>Notes A<br><span', vaults)
        self.assertIn('font-size:12px;color:#6e7781">/private/notes-a</span>', vaults)
        self.assertIn('font-size:14px;overflow-wrap:anywhere">2</td>', vaults)
        self.assertIn('font-size:14px;overflow-wrap:anywhere">2024-01-01 10:00:00</td>', vaults)
        self.assertNotIn('<li', vaults)
        self.assertIn('<h3>Vaults</h3><p>–</p>', render('i', 'h', {'Memory': rows[:5]}))

    def test_footer_uses_local_zone(self):
        from datetime import datetime, timedelta, timezone
        from unittest.mock import patch
        for name, offset in (('AEST', 10), ('NZDT', 13)):
            with self.subTest(name=name), patch('rendering.datetime') as clock:
                clock.now.return_value.astimezone.return_value = datetime(2026, 11, 20, tzinfo=timezone(timedelta(hours=offset), name))
                page = render('i', 'h', {}, report_id='example')
                self.assertIn(f'<td>Report ID: example</td><td align="right">Dates and times in {name}</td>', page)

    def test_plugin_profile_coverage_uses_all_profiles_and_exceptions(self):
        profiles = [f'profile-{n}' for n in range(9)]
        def fact(item, profile, notes='current'):
            return dict(status='ok', item=item, profile=profile, notes=notes, version='1')

        rows = [fact('everywhere', name, 'pinned' if name == profiles[-1] else 'current') for name in profiles]
        rows += [fact('almost', name) for name in profiles[:7]]
        rows += [fact('few', name) for name in profiles[:6]]
        page = render('i', 'h', {'Plugins': rows, 'Memory': [fact('MEMORY.md', name) for name in profiles]})
        plugins = page.split('3. Plugins</h2>', 1)[1].split('</table>', 1)[0]
        self.assertIn('>all profiles</span>', plugins)
        self.assertIn('>7 profiles (except profile-7, profile-8)</span>', plugins)
        self.assertIn('>6 profiles (except profile-6, profile-7, profile-8)</span>', plugins)
        self.assertIn('8 profiles (except profile-8)', plugins)

    def test_approval_is_conditional_and_numbered(self):
        empty = render('i', 'h', {})
        self.assertNotIn('Approval requested', empty)
        self.assertNotIn('approved', empty)
        page = render('i', 'h', {}, actions=['Restart: one minute downtime', 'Check'])
        self.assertIn('What I can do for you', page)
        self.assertIn('>1</b>', page)
        self.assertIn('>2</b>', page)
        self.assertIn('Restart: one minute downtime', page)
        self.assertIn('approved', page)

    def test_maintenance_and_all_statuses(self):
        rows = [{'status': s, 'item': s, 'version': '', 'notes': ''}
                for s in ('ok', 'info', 'warn', 'fail')]
        page = render('i', 'h', {'Memory': rows}, kind='maintenance')
        self.assertIn('Maintenance results', page)
        for symbol in ('✅', 'ℹ️', '⚠️', '❌'):
            self.assertIn(symbol, page)
        self.assertIn('prefers-color-scheme:dark', page)
        self.assertIn('rgb(22,30,63)', page)
        self.assertIn('Action needed. Something failed. See: Memory.', page)

    def test_compact_memory_and_plugin_version(self):
        rows = [dict(status='ok', item='MEMORY.md', profile='a', version='', notes='on; 97% of character limit', enabled=True, percent_used=97),
                dict(status='info', item='USER.md', profile='a', version='', notes='on; file unavailable')]
        plugin = dict(status='ok', item='rea', profile='a', version='a' * 40, notes='current')
        page = render('i', 'h', {'Memory': rows, 'Plugins': [plugin]})
        self.assertIn('✅ 97%', page)
        self.assertIn('✅ –', page)
        self.assertNotIn('of character limit', page)
        self.assertIn('text-overflow:ellipsis', page)
        self.assertIn('title="' + 'a' * 40 + '"', page)
        self.assertIn('table-layout:fixed', page)

    def test_coverage_prefers_shorter_group(self):
        from rendering import _profile_coverage
        self.assertEqual(_profile_coverage({'a', 'b', 'c'}, {'a', 'b', 'c', 'd'}), '3 profiles (except d)')

    def test_system_detail_hook_and_memory_exclusion(self):
        from unittest.mock import patch
        row = dict(status='ok', item='item', version='', notes='ordinary')
        with patch('rendering._system_detail', return_value=('special headline', '<b>special body</b>')) as hook:
            page = render('i', 'h', {'Backup': [row], 'Memory': [row], 'Plugins': [row]})
        self.assertIn('special headline', page)
        self.assertIn('<b>special body</b>', page)
        self.assertNotIn('Memory', [call.args[0] for call in hook.call_args_list])
        self.assertNotIn('Plugins', [call.args[0] for call in hook.call_args_list])

    def test_memory_disabled_and_legacy_notes(self):
        rows = [dict(status='ok', item='MEMORY.md', profile='a', version='', notes='on; 12% used'),
                dict(status='info', item='USER.md', profile='a', version='', notes='off')]
        page = render('i', 'h', {'Memory': rows})
        self.assertIn('✅ 12%', page)
        self.assertIn('⛔ –', page)

    def test_invalid_status_section_and_kind_are_rejected(self):
        with self.assertRaises(ValueError):
            render('i', 'h', {'Memory': [{'status': 'broken'}]})
        with self.assertRaises(ValueError):
            render('i', 'h', {'Unexpected': []})
        with self.assertRaises(ValueError):
            render('i', 'h', {}, kind='other')


if __name__ == '__main__':
    unittest.main()
